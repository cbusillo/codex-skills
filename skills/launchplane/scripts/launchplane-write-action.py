#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Execute bounded Launchplane operator actions with public-safe output."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
from launchplane_contract import helper_command_path  # noqa: E402
from launchplane_contract import internal_helper_path  # noqa: E402
from launchplane_safety import (  # noqa: E402
    LaunchplaneSafetyError,
    assert_public_safe_shape,
    build_launchplane_url,
    is_denied_key,
    public_code,
    public_identifier,
    public_summary_string,
    public_timestamp,
    public_trace_id,
    public_url,
    safe_urlopen,
    validate_service_url,
)


SCHEMA_VERSION = "1.0"
PROVIDER = "launchplane"
DEFAULT_CONFIG_PATH = Path("~/.config/launchplane/local-operator.json").expanduser()
DEFAULT_ENV_PATH = Path("~/.config/launchplane/local-operator.env").expanduser()
WRITE_CONFIG_REQUIRED = "Launchplane operator config is required for this write action."
LOCAL_OPERATOR_ENV_KEYS = {
    "LAUNCHPLANE_OPERATOR_URL",
    "LAUNCHPLANE_PUBLIC_URL",
    "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN",
    "LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT",
    "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL",
}
READ_ONLY_OPERATIONS = {
    "change-impact-policy-read",
    "repository-inventory-read",
    "integration-allowances-read",
    "testing-hold-read",
    "product-environment-read",
    "product-activity-read",
    "product-profile-read",
    "path-check",
    "preview-history-read",
    "reconcile-requests-read",
    "product-secret-bindings-read",
    "target-replacement-operation-read",
    "target-replacement-plan-read",
    "production-backup-authority-read",
    "private-health-endpoint-read",
    "product-promotion-status-read",
}
MERGE_TRAIN_POLICY_IMPORT_ENVELOPE_FIELDS = {
    "schema_version",
    "product",
    "mode",
    "reason",
    "record",
}
MERGE_TRAIN_POLICY_RECORD_FIELDS = {
    "schema_version",
    "record_id",
    "status",
    "source",
    "updated_at",
    "policy_sha256",
    "policy",
}
MERGE_TRAIN_POLICY_TARGETS_FIELDS = {"status", "trace_id", "policy", "targets"}
MERGE_TRAIN_POLICY_SUMMARY_FIELDS = {"record_id", "updated_at", "policy_sha256"}
MERGE_TRAIN_POLICY_TARGET_FIELDS = {
    "repository",
    "base_branch",
    "policy_key",
    "scheduler",
    "service_authz",
}
MERGE_TRAIN_SCHEDULER_FIELDS = {"enabled", "runner_mode", "mutate"}
MERGE_TRAIN_SERVICE_AUTHZ_FIELDS = {"action", "product", "context"}
MERGE_TRAIN_POLICY_IMPORT_RESULT_FIELDS = {"mode", "record"}
MERGE_TRAIN_POLICY_IMPORT_RECORD_FIELDS = {
    "record_id",
    "status",
    "source",
    "updated_at",
    "policy_sha256",
    "repository_count",
    "policy_keys",
}
ATTENTION_CONTROLLER_ACTIONS = {
    "batch_landed",
    "candidate_failed",
    "stack_unsupported",
    "block",
    "update_branch",
    "wait_for_checks",
    "wait_for_root_checks",
    "idle",
}
SUCCESS_TOP_LEVEL_KEYS = {
    "schema_version",
    "status",
    "trace_id",
    "records",
    "result",
    "replayed",
    "original_trace_id",
}
REPOSITORY_INVENTORY_RECORD_FIELDS = {
    "schema_version",
    "record_id",
    "repository_id",
    "repository_owner_id",
    "repository",
    "inventory_state",
    "inventory_revision",
    "recorded_at",
    "source",
    "reason",
    "supersedes_record_id",
    "inventory_digest",
}
REPOSITORY_INVENTORY_READ_FIELDS = {
    "schema_version",
    "status",
    "repository_id",
    "current_record",
    "history_count",
    "generated_at",
}
REPOSITORY_INVENTORY_APPLY_RESULT_FIELDS = {
    "schema_version",
    "status",
    "mode",
    "repository_id",
    "inventory_revision",
    "record_id",
    "inventory_digest",
    "supersedes_record_id",
    "applied_at",
}
MERGE_TRAIN_RESULT_FIELDS = {
    "active_action",
    "active_phase",
    "active_pull_request_number",
    "active_record_id",
    "base_branch",
    "batch_id",
    "blocking_reason",
    "branch_update_result",
    "candidate",
    "candidate_record_id",
    "candidate_ref",
    "candidate_ref_cleanup_github_status_code",
    "candidate_ref_cleanup_message",
    "candidate_ref_cleanup_status",
    "candidate_sha",
    "candidate_status",
    "cleanup_status",
    "code",
    "collapse_id",
    "completed_disposition_count",
    "completed_entry_count",
    "completed_mutation_count",
    "controller_action",
    "controller_reconciliation_status",
    "details",
    "dry_run_result",
    "entries",
    "error",
    "github_status_code",
    "heartbeat_at",
    "landing_plan",
    "landing_plan_record_id",
    "landing_sha",
    "last_action",
    "last_phase",
    "last_pull_request_number",
    "last_record_id",
    "lease_acquired_at",
    "lease_expires_at",
    "lease_owner",
    "merge_readiness",
    "merge_train_batch_candidate_record_id",
    "merge_train_batch_landing_plan_record_id",
    "merge_train_stack_collapse_plan_record_id",
    "message",
    "mode",
    "mutate",
    "pull_requests",
    "reason_code",
    "reconciliation_detail",
    "reconciliation_status",
    "release_error_type",
    "replacement_candidate_record_id",
    "repository",
    "root_merge_commit",
    "root_merge_commit_sha",
    "source_of_truth_url",
    "stack_collapse_plan",
    "stack_collapse_plan_record_id",
    "stack_discovery",
    "status",
    "step_payload",
    "structural_provenance",
    "superseded_candidate_record_id",
    "superseded_merge_train_batch_candidate_record_id",
    "trace_id",
    "updated_at",
    "workflow_run_url",
}
MERGE_TRAIN_BLOCKING_REASON_FIELDS = {"code", "message"}
MERGE_TRAIN_READINESS_FIELDS = {
    "state",
    "reason_codes",
    "owner_states",
    "technical_checks_state",
    "engineering_review_state",
    "policy_state",
    "candidate_state",
    "fence_state",
}
MERGE_TRAIN_STRUCTURAL_FIELDS = {
    "status",
    "reason_codes",
    "effective_base_sha",
    "effective_base_tree_sha",
    "candidate_sha256",
    "landing_plan_sha256",
    "provenance_sha256",
}
PRODUCT_CONFIG_INTENT_FIELDS = {
    "schema_version",
    "intent",
    "mode",
    "status",
    "authz_action",
    "product",
    "context",
    "source_url",
    "reason_code",
    "safe_to_execute",
    "next_action",
    "audit",
    "secret_evidence",
}
PRODUCT_CONFIG_PREFLIGHT_RESULT_FIELDS = {
    "intent",
    "record",
    "binding_keys",
    "secret_binding_keys",
    "managed_secret_binding_keys",
    "runtime_key_safety_findings",
    "key_safety_findings",
}
PRODUCT_CONFIG_APPLY_RESULT_FIELDS = {
    "status",
    "mode",
    "product",
    "context",
    "instance",
    "actor",
    "source_label",
    "reason",
    "runtime_environment",
    "runtime_key_safety",
    "secrets",
    "provider_key_adoption",
    "summary",
    "next_actions",
}
PROVIDER_KEY_ADOPTION_DISPOSITIONS = {
    "adopted",
    "template_default",
    "already_recorded",
    "refused_credential",
    "missing",
}
PREVIEW_FEEDBACK_REMEDIATION_RESULT_FIELDS = {
    "schema_version",
    "remediation_id",
    "product",
    "context",
    "repository",
    "pull_request_url",
    "pull_request_number",
    "mode",
    "terminal_status",
    "actor",
    "reason",
    "related_issue",
    "trace_id",
    "idempotency_key",
    "requested_at",
    "continuity_sha256",
    "observation",
    "planned_action",
    "outcome",
    "mutation_evidence",
    "companion_feedback_id",
}
CHANGE_IMPACT_POLICY_RESULT_FIELDS = {"schema_version", "status", "record", "audit", "attribution_status"}
CHANGE_IMPACT_POLICY_RECORD_FIELDS = {
    "schema_version",
    "record_id",
    "status",
    "repository_id",
    "repository_owner_id",
    "repository",
    "policy_revision",
    "component_rules",
    "default_unknown_review_tier",
    "effective_at",
    "source",
    "reason",
    "supersedes_record_id",
    "policy_digest",
    "classification_model",
}
CHANGE_IMPACT_COMPONENT_RULE_FIELDS = {
    "schema_version",
    "rule_id",
    "component",
    "path_prefixes",
    "affected_products",
    "review_tier",
    "production_affecting",
    "product_impact",
    "governance_impact",
    "generated_by",
    "reason",
}
CHANGE_IMPACT_PRODUCT_SCOPE_FIELDS = {
    "schema_version",
    "product",
    "system",
    "owner_action",
    "owner_environment",
}
CHANGE_IMPACT_POLICY_READ_FIELDS = {
    "schema_version",
    "mode",
    "authoritative",
    "enforcement_effect",
    "repository_id",
    "current_policy",
    "policy_history_count",
    "audit",
    "attribution_status",
}
CHANGE_IMPACT_POLICY_AUDIT_FIELDS = {
    "schema_version", "record_id", "policy_digest", "actor_kind", "actor_subject",
    "workflow_identity", "trace_id", "recorded_at",
}
CHANGE_IMPACT_POLICY_WORKFLOW_FIELDS = {
    "repository", "repository_id", "repository_owner_id", "workflow_ref",
    "job_workflow_ref", "ref", "sha",
}
GENERIC_WEB_DEPLOY_RECOVERY_DRY_RUN_FIELDS = {
    "schema_version",
    "status",
    "mode",
    "product",
    "context",
    "instance",
    "reservation_state",
    "reservation_attempt",
    "reservation_created_at",
    "reservation_updated_at",
    "reservation_lease_expires_at",
    "observed_at",
    "reconciliation_key_sha256",
    "provider_target_key_sha256",
    "provider_effect_phase",
    "provider_outcome",
    "provider_status",
    "retry_safe",
    "proposed_action",
    "recovery_digest",
}
GENERIC_WEB_DEPLOY_RECOVERY_APPLY_FIELDS = {
    "schema_version",
    "status",
    "mode",
    "trace_id",
    "product",
    "context",
    "instance",
    "reservation_state",
    "reservation_attempt",
    "recovery_action",
    "recovery_digest",
    "provider_outcome",
    "provider_status",
    "retry_safe",
}


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def active_repo_root() -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
        root = result.stdout.strip()
        if root:
            return Path(root).resolve(strict=True)
    except (OSError, subprocess.CalledProcessError):
        pass
    return Path.cwd().resolve(strict=True)


def absolute_path_without_symlink_resolution(path: Path) -> Path:
    if path.is_absolute():
        return Path(os.path.abspath(path))
    return Path(os.path.abspath(Path.cwd() / path))


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def warning(code: str, message: str) -> dict[str, str]:
    return dict(code=code, message=message)


def emit(payload: dict[str, object]) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def base_payload(*, status: str, operation: str, request: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "provider": PROVIDER,
        "operation": operation,
        "generated_at": utc_now(),
        "request": request,
        "summary": {},
        "records": {},
        "result": {},
        "warnings": [],
    }


def no_context_payload(
    *,
    operation: str,
    request: dict[str, object],
    code: str = "missing_operator_config",
    message: str = WRITE_CONFIG_REQUIRED,
    recommendation: str = "Configure Launchplane operator access before retrying.",
) -> dict[str, object]:
    payload = base_payload(status="no_context", operation=operation, request=request)
    payload["summary"] = {"configuration_state": code, "recommendation": recommendation}
    payload["warnings"] = [warning(code, message)]
    return payload


def unavailable_payload(
    *, operation: str, request: dict[str, object], status: str, code: str, message: str
) -> dict[str, object]:
    payload = base_payload(status=status, operation=operation, request=request)
    payload["summary"] = {"recommendation": "Stop and inspect the Launchplane trace before retrying."}
    payload["warnings"] = [warning(code, message)]
    return payload


def load_config(path: str | None) -> dict[str, str]:
    config: dict[str, str] = {}
    config_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return config
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ValueError("invalid_config") from None
    if not isinstance(raw, dict):
        raise ValueError("invalid_config")
    for key in (
        "service_url",
        "operator_token_env",
        "operator_subject_env",
        "operator_token_label_env",
    ):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            config[key] = value.strip()
    return config


def load_operator_env(path: str | None = None) -> dict[str, str]:
    env_path = Path(path).expanduser() if path else DEFAULT_ENV_PATH
    if not env_path.exists():
        return {}
    loaded: dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise ValueError("invalid_env_config") from None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].strip()
        key, separator, value = stripped.partition("=")
        if separator != "=":
            continue
        key = key.strip()
        if key not in LOCAL_OPERATOR_ENV_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if value:
            loaded[key] = value
    return loaded


def load_operator_sources(
    args: argparse.Namespace,
) -> tuple[dict[str, str], dict[str, str], str]:
    config = load_config(args.config)
    env_config = (
        load_operator_env(args.env_config)
        if args.env_config
        else {}
        if args.config
        else load_operator_env()
    )
    if args.config:
        service_url = (
            args.url
            or config.get("service_url")
            or os.environ.get("LAUNCHPLANE_OPERATOR_URL")
            or ""
        ).strip()
    else:
        service_url = (
            args.url
            or os.environ.get("LAUNCHPLANE_OPERATOR_URL")
            or env_config.get("LAUNCHPLANE_OPERATOR_URL")
            or config.get("service_url")
            or ""
        ).strip()
    return config, env_config, service_url


def resolve_settings(args: argparse.Namespace) -> dict[str, str]:
    config, env_config, service_url = load_operator_sources(args)
    token_env = (config.get("operator_token_env") or "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN").strip()
    subject_env = (
        config.get("operator_subject_env") or "LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT"
    ).strip()
    token_label_env = (
        config.get("operator_token_label_env") or "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL"
    ).strip()
    return {
        "service_url": service_url,
        "token": (os.environ.get(token_env) or env_config.get(token_env) or "").strip(),
        "subject": (os.environ.get(subject_env) or env_config.get(subject_env) or "").strip(),
        "token_label": (os.environ.get(token_label_env) or env_config.get(token_label_env) or "").strip(),
        "public_url_hint_sources": ",".join(public_url_hint_sources(env_config)),
    }


def public_url_hint_sources(env_config: dict[str, str]) -> list[str]:
    sources: list[str] = []
    if os.environ.get("LAUNCHPLANE_PUBLIC_URL"):
        sources.append("environment")
    if env_config.get("LAUNCHPLANE_PUBLIC_URL"):
        sources.append("private_env")
    return sources


def classify_operator_config(
    *, service_url_present: bool, token_present: bool, public_url_hint_present: bool
) -> str:
    if service_url_present and token_present:
        return "ready"
    if not service_url_present and token_present and public_url_hint_present:
        return "ambiguous_service_url"
    if not service_url_present and token_present:
        return "missing_service_url"
    if service_url_present and not token_present:
        return "missing_operator_token"
    return "missing_operator_config"


def operator_config_recommendation(classification: str) -> str:
    if classification.startswith("invalid_service_url"):
        return (
            "Fix the Launchplane operator URL source. It must be an absolute HTTPS URL; "
            "HTTP is allowed only for explicit loopback hosts."
        )
    recommendations = {
        "ready": "Operator config sources are present; proceed only through supported helper commands.",
        "ambiguous_service_url": (
            "LAUNCHPLANE_PUBLIC_URL is present but not used as the operator URL; obtain "
            "the correct operator URL and pass --url before the subcommand, or configure "
            "LAUNCHPLANE_OPERATOR_URL."
        ),
        "missing_service_url": (
            "Local operator token material is present, but no write-capable "
            "Launchplane service URL source was found. Configure "
            "LAUNCHPLANE_OPERATOR_URL or pass --url before the subcommand, then "
            "rerun operator-config-diagnostic."
        ),
        "missing_operator_token": "Configure the local operator token source before retrying.",
        "missing_operator_config": "Configure Launchplane operator URL and token sources before retrying.",
    }
    return recommendations.get(classification, recommendations["missing_operator_config"])


def operator_config_message(classification: str) -> str:
    if classification.startswith("invalid_service_url"):
        return "Launchplane operator service URL is invalid."
    messages = {
        "ambiguous_service_url": (
            "A public Launchplane URL source is present, but no operator URL source is configured."
        ),
        "missing_service_url": "Launchplane operator service URL is missing.",
        "missing_operator_token": "Launchplane local operator token is missing.",
        "missing_operator_config": WRITE_CONFIG_REQUIRED,
    }
    return messages.get(classification, WRITE_CONFIG_REQUIRED)


def settings_diagnostic(args: argparse.Namespace) -> dict[str, object]:
    config = load_config(args.config)
    env_config = load_operator_env(args.env_config) if args.env_config else {} if args.config else load_operator_env()
    token_env = (config.get("operator_token_env") or "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN").strip()
    subject_env = (
        config.get("operator_subject_env") or "LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT"
    ).strip()
    token_label_env = (
        config.get("operator_token_label_env") or "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL"
    ).strip()
    service_url_candidates = (
        (
            ("argument", args.url or ""),
            ("json_config", config.get("service_url") or ""),
            ("environment", os.environ.get("LAUNCHPLANE_OPERATOR_URL") or ""),
        )
        if args.config
        else (
            ("argument", args.url or ""),
            ("environment", os.environ.get("LAUNCHPLANE_OPERATOR_URL") or ""),
            ("private_env", env_config.get("LAUNCHPLANE_OPERATOR_URL") or ""),
            ("json_config", config.get("service_url") or ""),
        )
    )
    service_url_sources = [source for source, value in service_url_candidates if value]
    public_hint_sources = public_url_hint_sources(env_config)
    token_present = bool(os.environ.get(token_env) or env_config.get(token_env))
    classification = classify_operator_config(
        service_url_present=bool(service_url_sources),
        token_present=token_present,
        public_url_hint_present=bool(public_hint_sources),
    )
    winning_service_url = next((value for _source, value in service_url_candidates if value), "")
    if winning_service_url:
        try:
            validate_service_url(winning_service_url)
        except LaunchplaneSafetyError as exc:
            classification = exc.code
    return {
        "classification": classification,
        "ready": classification == "ready",
        "json_config_present": (Path(args.config).expanduser() if args.config else DEFAULT_CONFIG_PATH).exists(),
        "private_env_present": (Path(args.env_config).expanduser() if args.env_config else DEFAULT_ENV_PATH).exists(),
        "service_url_sources": service_url_sources,
        "service_url_source": service_url_sources[0] if service_url_sources else "missing",
        "public_url_hint_sources": public_hint_sources,
        "public_url_hint_present": bool(public_hint_sources),
        "token_present": token_present,
        "token_source": "environment" if os.environ.get(token_env) else "private_env" if env_config.get(token_env) else "missing",
        "subject_present": bool(os.environ.get(subject_env) or env_config.get(subject_env)),
        "token_label_present": bool(os.environ.get(token_label_env) or env_config.get(token_label_env)),
        "recommendation": operator_config_recommendation(classification),
    }


def build_url(service_url: str, path: str) -> str:
    return build_launchplane_url(service_url, path)


def request_launchplane(
    *,
    service_url: str,
    path: str,
    settings: dict[str, str],
    body: dict[str, object],
    timeout: float,
    idempotency_key: str = "",
) -> dict[str, Any]:
    data = json.dumps(body, separators=(",", ":")).encode()
    request = urllib.request.Request(build_url(service_url, path), data=data, method="POST")
    request.add_header("Accept", "application/json")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {settings['token']}")
    if idempotency_key:
        request.add_header("Idempotency-Key", idempotency_key)
    with safe_urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid_response")
    return payload


def request_launchplane_read(
    *,
    service_url: str,
    path: str,
    settings: dict[str, str],
    query: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    encoded_query = urllib.parse.urlencode(query)
    request = urllib.request.Request(
        build_launchplane_url(service_url, path, query=encoded_query), method="GET"
    )
    request.add_header("Accept", "application/json")
    request.add_header("Authorization", f"Bearer {settings['token']}")
    with safe_urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid_response")
    return payload


def _require_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raise LaunchplaneSafetyError("invalid_response")


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is not None:
        raise LaunchplaneSafetyError("invalid_response")
    return None


def _optional_public_identifier(value: object) -> str | None:
    if not isinstance(value, str):
        raise LaunchplaneSafetyError("invalid_response")
    if not value.strip():
        return None
    return public_identifier(value)


def _project_records(records: object, allowed_keys: set[str]) -> dict[str, object]:
    if records is None:
        return {}
    source = _require_dict(records)
    projected: dict[str, object] = {}
    for key, value in source.items():
        key = str(key)
        if key not in allowed_keys or is_denied_key(key):
            raise LaunchplaneSafetyError("unsafe_response_shape")
        projected[key] = public_identifier(value)
    return projected


def _public_string_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LaunchplaneSafetyError("invalid_response")
    return [public_identifier(item) for item in value]


def _public_code_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LaunchplaneSafetyError("invalid_response")
    projected: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise LaunchplaneSafetyError("invalid_response")
        projected.append(public_code(item))
    return projected


def _nonnegative_int(value: object) -> int:
    if not isinstance(value, int):
        raise LaunchplaneSafetyError("invalid_response")
    if isinstance(value, bool) or value < 0:
        raise LaunchplaneSafetyError("invalid_response")
    return cast(int, value)


def _project_key_safety_findings(value: object) -> list[dict[str, object]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LaunchplaneSafetyError("invalid_response")
    projected: list[dict[str, object]] = []
    for item in value:
        source = _require_dict(item)
        allowed = {
            "key",
            "binding_key",
            "binding_id",
            "secret_id",
            "secret_class",
            "detail",
            "code",
            "reason_code",
            "severity",
            "status",
        }
        if any(str(key) not in allowed for key in source):
            raise LaunchplaneSafetyError("unsafe_response_shape")
        finding: dict[str, object] = {}
        binding_key = source.get("key") or source.get("binding_key")
        if binding_key:
            finding["key"] = public_identifier(binding_key)
        for key in ("code", "reason_code", "severity", "status"):
            if key in source:
                finding[key] = public_code(source[key])
        projected.append(finding)
    return projected


def _project_key_safety_summary(source: dict[str, Any]) -> dict[str, object]:
    projected: dict[str, object] = {}
    if "status" in source:
        projected["status"] = public_code(source["status"])
    if "checked_binding_keys" in source:
        projected["checked_binding_keys"] = _public_string_list(
            source["checked_binding_keys"]
        )
    if "findings" in source:
        projected["findings"] = _project_key_safety_findings(source["findings"])
    # Findings Launchplane reports without refusing, such as a shared production
    # key bound before sharing reasons existed.
    if source.get("reported"):
        projected["reported"] = _project_key_safety_findings(source["reported"])
    return projected


def _project_secret_evidence(value: object) -> dict[str, object]:
    source = _require_dict(value)
    allowed = {
        "status",
        "destination",
        "checked_binding_keys",
        "policy_record_id",
        "policy_sha256",
        "findings",
    }
    if any(str(key) not in allowed for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    destination = source.get("destination")
    if destination is not None:
        destination_source = _require_dict(destination)
        if any(
            str(key) not in {"kind", "context", "instance"}
            for key in destination_source
        ):
            raise LaunchplaneSafetyError("unsafe_response_shape")
    return _project_key_safety_summary(source)


def _project_intent_evaluation(value: object) -> dict[str, object]:
    source = _require_dict(value)
    if any(str(key) not in PRODUCT_CONFIG_INTENT_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    audit = source.get("audit")
    if audit is not None:
        audit_source = _require_dict(audit)
        if any(
            str(key)
            not in {
                "decision",
                "reason_code",
                "subject",
                "action",
                "product",
                "context",
                "policy_source",
                "policy_sha256",
                "source_kind",
            }
            for key in audit_source
        ):
            raise LaunchplaneSafetyError("unsafe_response_shape")
    projected: dict[str, object] = {}
    for key in ("intent", "mode", "status", "reason_code"):
        if key in source:
            projected[key] = public_code(source[key])
    for key in ("authz_action", "product", "context"):
        if key in source:
            projected[key] = public_identifier(source[key])
    if source.get("source_url"):
        projected["source_url"] = public_url(source["source_url"])
    safe_to_execute = _optional_bool(source.get("safe_to_execute"))
    if safe_to_execute is not None:
        projected["safe_to_execute"] = safe_to_execute
    if "next_action" in source:
        projected["next_action"] = public_summary_string(source["next_action"])
    if "secret_evidence" in source:
        projected["secret_evidence"] = _project_secret_evidence(
            source["secret_evidence"]
        )
    return projected


def _project_product_config_preflight_result(result: object) -> dict[str, object]:
    source = _require_dict(result)
    if any(str(key) not in PRODUCT_CONFIG_PREFLIGHT_RESULT_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    projected: dict[str, object] = {}
    if "intent" in source:
        projected["intent"] = _project_intent_evaluation(source["intent"])
    if "record" in source:
        record = _require_dict(source["record"])
        if any(str(key) not in {"record_id", "recorded_at"} for key in record):
            raise LaunchplaneSafetyError("unsafe_response_shape")
        projected_record: dict[str, object] = {}
        if "record_id" in record:
            projected_record["record_id"] = public_identifier(record["record_id"])
        if "recorded_at" in record:
            projected_record["recorded_at"] = public_timestamp(record["recorded_at"])
        projected["record"] = projected_record
    for source_key, target_key in (
        ("binding_keys", "binding_keys"),
        ("secret_binding_keys", "secret_binding_keys"),
        ("managed_secret_binding_keys", "managed_secret_binding_keys"),
    ):
        if source_key in source:
            projected[target_key] = _public_string_list(source[source_key])
    for source_key, target_key in (
        ("runtime_key_safety_findings", "runtime_key_safety_findings"),
        ("key_safety_findings", "key_safety_findings"),
    ):
        if source_key in source:
            projected[target_key] = _project_key_safety_findings(source[source_key])
    assert_public_safe_shape(projected)
    return projected


def _project_retired_provider_keys(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 256:
        raise LaunchplaneSafetyError("invalid_response")
    keys = _public_string_list(value)
    if len(set(keys)) != len(keys) or any(
        re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", key) is None for key in keys
    ):
        raise LaunchplaneSafetyError("invalid_response")
    return keys


def _project_runtime_environment(value: object) -> dict[str, object]:
    source = _require_dict(value)
    allowed = {
        "action",
        "scope",
        "context",
        "instance",
        "keys",
        "changed_keys",
        "unchanged_keys",
        "env_value_count_after",
        "retired_provider_keys_before",
        "retired_provider_keys_after",
        "record",
    }
    if any(str(key) not in allowed for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    if not {"action", "scope", "context", "instance"}.issubset(source):
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {"action": public_identifier(source["action"])}
    scope = public_identifier(source["scope"])
    if scope not in {"global", "context", "instance"}:
        raise LaunchplaneSafetyError("invalid_response")
    projected["scope"] = scope
    context = _optional_public_identifier(source["context"])
    instance = _optional_public_identifier(source["instance"])
    if scope == "global" and (context is not None or instance is not None):
        raise LaunchplaneSafetyError("invalid_response")
    if scope == "context" and (context is None or instance is not None):
        raise LaunchplaneSafetyError("invalid_response")
    if scope == "instance" and (context is None or instance is None):
        raise LaunchplaneSafetyError("invalid_response")
    if context is not None:
        projected["context"] = context
    if instance is not None:
        projected["instance"] = instance
    for key in ("keys", "changed_keys", "unchanged_keys"):
        if key in source:
            projected[key] = _public_string_list(source[key])
    for key in ("retired_provider_keys_before", "retired_provider_keys_after"):
        if key in source:
            retired_keys = _project_retired_provider_keys(source[key])
            if retired_keys and scope != "instance":
                raise LaunchplaneSafetyError("invalid_response")
            projected[key] = retired_keys
    if "env_value_count_after" in source:
        projected["env_value_count_after"] = _nonnegative_int(
            source["env_value_count_after"]
        )
    record = source.get("record")
    if record is not None:
        record_source = _require_dict(record)
        if any(
            str(key)
            not in {
                "scope",
                "context",
                "instance",
                "updated_at",
                "source_label",
                "env_keys",
                "env_value_count",
                "retired_provider_keys",
            }
            for key in record_source
        ):
            raise LaunchplaneSafetyError("unsafe_response_shape")
        if "retired_provider_keys" in record_source:
            retired_keys = _project_retired_provider_keys(record_source["retired_provider_keys"])
            if retired_keys and scope != "instance":
                raise LaunchplaneSafetyError("invalid_response")
    return projected


def _project_runtime_key_safety(value: object) -> dict[str, object]:
    source = _require_dict(value)
    allowed = {
        "required",
        "status",
        "policy_record_id",
        "policy_sha256",
        "target",
        "checked_binding_keys",
        "findings",
        "reported",
    }
    if any(str(key) not in allowed for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    target = source.get("target")
    if target is not None and any(
        str(key) not in {"context", "instance", "environment_class"}
        for key in _require_dict(target)
    ):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    projected = _project_key_safety_summary(source)
    required = _optional_bool(source.get("required"))
    if required is not None:
        projected["required"] = required
    return projected


SECRET_COPY_FROM_FIELDS = ("context", "instance", "version_id")


def _project_secret_copy_from(value: object) -> dict[str, object]:
    """The source a copied secret came from, as reviewed: lane and version id only."""
    source = _require_dict(value)
    if set(source) != set(SECRET_COPY_FROM_FIELDS):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    return {key: public_identifier(source[key]) for key in SECRET_COPY_FROM_FIELDS}


def _project_secret_results(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise LaunchplaneSafetyError("invalid_response")
    projected: list[dict[str, object]] = []
    allowed = {
        "action",
        "scope",
        "integration",
        "name",
        "binding_key",
        "context",
        "instance",
        "secret_id",
        "secret_class",
        "sharing_reason",
        "copy_from",
    }
    for item in value:
        source = _require_dict(item)
        if any(str(key) not in allowed for key in source):
            raise LaunchplaneSafetyError("unsafe_response_shape")
        result: dict[str, object] = {}
        for key in ("action", "integration", "binding_key"):
            if key in source:
                result[key] = public_identifier(source[key])
        if "secret_class" in source:
            result["secret_class"] = public_code(source["secret_class"], default="unknown")
        if source.get("sharing_reason") is not None:
            result["sharing_reason"] = _project_sharing_reason(source["sharing_reason"])
        if source.get("copy_from") is not None:
            result["copy_from"] = _project_secret_copy_from(source["copy_from"])
        projected.append(result)
    return projected


def _project_provider_key_adoption(value: object) -> list[dict[str, str]]:
    """Each adopted key's name and disposition; the service never sends a value."""
    if not isinstance(value, list) or len(value) > 256:
        raise LaunchplaneSafetyError("invalid_response")
    projected: list[dict[str, str]] = []
    for item in value:
        source = _require_dict(item)
        if set(source) != {"key", "disposition"}:
            raise LaunchplaneSafetyError("unsafe_response_shape")
        key = source["key"]
        disposition = source["disposition"]
        if (
            not isinstance(key, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key) is None
            or disposition not in PROVIDER_KEY_ADOPTION_DISPOSITIONS
        ):
            raise LaunchplaneSafetyError("invalid_response")
        projected.append({"key": key, "disposition": str(disposition)})
    if len({item["key"] for item in projected}) != len(projected):
        raise LaunchplaneSafetyError("invalid_response")
    return projected


def _project_apply_summary(value: object) -> dict[str, object]:
    source = _require_dict(value)
    allowed = {"runtime_changed_key_count", "secret_change_count"}
    if any(str(key) not in allowed for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    return {
        key: _nonnegative_int(source[key])
        for key in ("runtime_changed_key_count", "secret_change_count")
        if key in source
    }


def _project_next_actions(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise LaunchplaneSafetyError("invalid_response")
    projected: list[dict[str, object]] = []
    allowed = {
        "kind",
        "required",
        "status",
        "target",
        "changed_keys",
        "dry_run",
        "apply",
        "instruction",
    }
    for item in value:
        source = _require_dict(item)
        if any(str(key) not in allowed for key in source):
            raise LaunchplaneSafetyError("unsafe_response_shape")
        for nested_key, nested_allowed in (
            ("target", {"context", "instance", "target_type", "target_name"}),
            ("dry_run", {"method", "endpoint", "mode"}),
            ("apply", {"method", "endpoint", "mode"}),
        ):
            nested = source.get(nested_key)
            if nested is not None and any(
                str(key) not in nested_allowed for key in _require_dict(nested)
            ):
                raise LaunchplaneSafetyError("unsafe_response_shape")
        action: dict[str, object] = {}
        for key in ("kind", "status"):
            if key in source:
                action[key] = public_code(source[key])
        required = _optional_bool(source.get("required"))
        if required is not None:
            action["required"] = required
        if "changed_keys" in source:
            action["changed_keys"] = _public_string_list(source["changed_keys"])
        projected.append(action)
    return projected


def _project_product_config_apply_result(result: object) -> dict[str, object]:
    source = _require_dict(result)
    if any(str(key) not in PRODUCT_CONFIG_APPLY_RESULT_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    required = {
        "status",
        "mode",
        "product",
        "context",
        "instance",
        "runtime_environment",
        "runtime_key_safety",
        "secrets",
        "summary",
        "next_actions",
    }
    if not required.issubset(source):
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {}
    for key in ("status", "mode"):
        if key in source:
            projected[key] = public_code(source[key])
    if "product" in source:
        projected["product"] = public_identifier(source["product"])
    context = _optional_public_identifier(source["context"])
    instance = _optional_public_identifier(source["instance"])
    runtime_environment = _project_runtime_environment(source["runtime_environment"])
    if context != runtime_environment.get("context") or instance != runtime_environment.get(
        "instance"
    ):
        raise LaunchplaneSafetyError("invalid_response")
    if context is not None:
        projected["context"] = context
    if instance is not None:
        projected["instance"] = instance
    projected["runtime_environment"] = runtime_environment
    for key, projector in (
        ("runtime_key_safety", _project_runtime_key_safety),
        ("secrets", _project_secret_results),
        ("provider_key_adoption", _project_provider_key_adoption),
        ("summary", _project_apply_summary),
        ("next_actions", _project_next_actions),
    ):
        if key in source:
            projected[key] = projector(source[key])
    assert_public_safe_shape(projected)
    return projected


def _project_merge_component(value: object) -> dict[str, object]:
    source = _require_dict(value)
    assert_public_safe_shape(source)
    projected: dict[str, object] = {}
    for key in (
        "status",
        "mode",
        "action",
        "controller_action",
        "candidate_sha",
        "base_sha",
        "candidate_ref",
        "root_merge_commit_sha",
        "record_id",
        "landing_plan_record_id",
        "collapse_id",
        "code",
    ):
        if key in source:
            value = source[key]
            if value is not None and value != "":
                projected[key] = public_identifier(value)
    for key in (
        "github_status_code",
        "completed_entry_count",
        "completed_disposition_count",
        "completed_mutation_count",
    ):
        if key in source:
            projected[key] = _nonnegative_int(source[key])
    for key in ("entries", "pull_requests", "child_dispositions"):
        if key in source:
            if not isinstance(source[key], list):
                raise LaunchplaneSafetyError("invalid_response")
            projected[f"{key}_count"] = len(source[key])
    return projected


def _merge_train_pr_number(value: object) -> int:
    number = _nonnegative_int(value)
    if number == 0:
        raise LaunchplaneSafetyError("invalid_response")
    return number


def _project_merge_train_queue_entry(value: object) -> dict[str, object]:
    source = _require_dict(value)
    projected: dict[str, object] = {
        "number": _merge_train_pr_number(source.get("number")),
        "head_sha": public_identifier(source.get("head_sha")),
    }
    for key in ("actor_role", "mergeable", "required_checks_status"):
        item = source.get(key)
        if not isinstance(item, str):
            raise LaunchplaneSafetyError("invalid_response")
        projected[key] = public_code(item)
    for key in ("eligible", "branch_update_required"):
        if not isinstance(source.get(key), bool):
            raise LaunchplaneSafetyError("invalid_response")
        projected[key] = source[key]
    reasons = source.get("ineligible_reasons")
    if not isinstance(reasons, list):
        raise LaunchplaneSafetyError("invalid_response")
    projected["ineligible_reasons"] = [public_summary_string(reason) for reason in reasons]
    return projected


def _project_merge_train_dry_run(value: object) -> dict[str, object]:
    source = _require_dict(value)
    projected = _project_merge_component(source)
    if "intended_next_action" in source:
        if not isinstance(source["intended_next_action"], str):
            raise LaunchplaneSafetyError("invalid_response")
        projected["intended_next_action"] = public_code(source["intended_next_action"])
    if "next_action_detail" in source:
        projected["next_action_detail"] = public_summary_string(source["next_action_detail"])
    if "queue_order" in source:
        order = source["queue_order"]
        if not isinstance(order, list):
            raise LaunchplaneSafetyError("invalid_response")
        projected["queue_order"] = [_merge_train_pr_number(number) for number in order]
    if "queue" in source:
        queue = source["queue"]
        if not isinstance(queue, list):
            raise LaunchplaneSafetyError("invalid_response")
        projected["queue"] = [_project_merge_train_queue_entry(entry) for entry in queue]
    if "selected_pr" in source:
        selected = source["selected_pr"]
        projected["selected_pr"] = (
            None if selected is None else _project_merge_train_queue_entry(selected)
        )
    return projected


def _project_merge_train_blocking_reason(value: object) -> dict[str, object]:
    source = _require_dict(value)
    if any(str(key) not in MERGE_TRAIN_BLOCKING_REASON_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    code = source.get("code")
    if not isinstance(code, str):
        raise LaunchplaneSafetyError("invalid_response")
    return {
        "code": public_code(code),
        "message": public_summary_string(source.get("message")),
    }


def _project_merge_train_readiness(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    source = _require_dict(value)
    if any(str(key) not in MERGE_TRAIN_READINESS_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    projected: dict[str, object] = {}
    for key in (
        "state",
        "technical_checks_state",
        "engineering_review_state",
        "policy_state",
        "candidate_state",
        "fence_state",
    ):
        if key in source:
            if not isinstance(source[key], str):
                raise LaunchplaneSafetyError("invalid_response")
            projected[key] = public_code(source[key])
    for key in ("reason_codes", "owner_states"):
        if key in source:
            projected[key] = _public_code_list(source[key])
    return projected


def _project_merge_train_structural_provenance(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    source = _require_dict(value)
    if any(str(key) not in MERGE_TRAIN_STRUCTURAL_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    projected: dict[str, object] = {}
    for key, item in source.items():
        if key == "reason_codes":
            projected[key] = _public_code_list(item)
        elif not isinstance(item, str):
            raise LaunchplaneSafetyError("invalid_response")
        elif key == "status":
            projected[key] = public_code(item)
        else:
            projected[key] = public_identifier(item) if item else ""
    return projected


def _project_merge_train_result(result: object) -> dict[str, object]:
    source = _require_dict(result)
    if any(str(key) not in MERGE_TRAIN_RESULT_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    assert_public_safe_shape(source)
    projected: dict[str, object] = {}
    for key in (
        "repository",
        "base_branch",
        "mode",
        "controller_action",
        "reason_code",
        "candidate_sha",
        "landing_sha",
        "root_merge_commit",
        "root_merge_commit_sha",
        "candidate_ref_cleanup_status",
        "controller_reconciliation_status",
        "status",
    ):
        if key in source:
            projected[key] = public_identifier(source[key])
    if "mutate" in source:
        projected["mutate"] = _optional_bool(source["mutate"])
    if "candidate_ref_cleanup_github_status_code" in source:
        projected["candidate_ref_cleanup_github_status_code"] = _nonnegative_int(
            source["candidate_ref_cleanup_github_status_code"]
        )
    if "blocking_reason" in source:
        projected["blocking_reason"] = _project_merge_train_blocking_reason(
            source["blocking_reason"]
        )
    if "merge_readiness" in source:
        projected["merge_readiness"] = _project_merge_train_readiness(
            source["merge_readiness"]
        )
    if "structural_provenance" in source:
        projected["structural_provenance"] = _project_merge_train_structural_provenance(
            source["structural_provenance"]
        )
    if "dry_run_result" in source:
        projected["dry_run_result"] = _project_merge_train_dry_run(source["dry_run_result"])
    for key in ("workflow_run_url", "source_of_truth_url"):
        if key in source:
            projected[key] = public_url(source[key])
    for key in (
        "candidate",
        "landing_plan",
        "stack_collapse_plan",
        "stack_discovery",
        "error",
        "details",
        "branch_update_result",
    ):
        if key in source:
            projected[key] = _project_merge_component(source[key])
    assert_public_safe_shape(projected)
    return projected


def _project_preview_feedback_remediation_result(result: object) -> dict[str, object]:
    source = _require_dict(result)
    if any(str(key) not in PREVIEW_FEEDBACK_REMEDIATION_RESULT_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    projected: dict[str, object] = {}
    for key in (
        "remediation_id",
        "product",
        "context",
        "repository",
        "terminal_status",
        "actor",
        "related_issue",
        "mode",
        "planned_action",
        "outcome",
        "requested_at",
    ):
        if key in source:
            projected[key] = public_summary_string(source[key])
    if source.get("companion_feedback_id"):
        projected["companion_feedback_id"] = public_identifier(
            source["companion_feedback_id"]
        )
    if "pull_request_number" in source:
        projected["pull_request_number"] = _nonnegative_int(source["pull_request_number"])
    if "pull_request_url" in source:
        projected["pull_request_url"] = public_url(source["pull_request_url"])
    observation = source.get("observation")
    if isinstance(observation, dict):
        projected_observation: dict[str, object] = {
            "state": public_code(observation.get("state"), default="unknown"),
            "comment_id": _nonnegative_int(observation.get("comment_id", 0)),
            "comment_author_login": public_identifier(
                observation.get("comment_author_login") or "unknown"
            ),
            "github_actor_login": public_identifier(
                observation.get("token_actor_login") or "unknown"
            ),
        }
        if observation.get("comment_url"):
            projected_observation["comment_url"] = public_url(
                observation["comment_url"]
            )
        projected["observation"] = projected_observation
    mutation = source.get("mutation_evidence")
    if isinstance(mutation, dict):
        projected["mutation_evidence"] = {
            "attempted": bool(mutation.get("attempted")),
            "mutated": bool(mutation.get("mutated")),
            "method": public_code(mutation.get("method"), default="none"),
            "comment_id": _nonnegative_int(mutation.get("comment_id", 0)),
            "verified_absent": bool(mutation.get("verified_absent")),
        }
    assert_public_safe_shape(projected)
    return projected


def _project_change_impact_policy_record(record_value: object) -> dict[str, object]:
    record = _require_dict(record_value)
    if any(str(key) not in CHANGE_IMPACT_POLICY_RECORD_FIELDS for key in record):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    if record.get("classification_model") not in (None, "v2"):
        raise LaunchplaneSafetyError("invalid_response")
    component_rules = record.get("component_rules")
    if component_rules is not None:
        if not isinstance(component_rules, list):
            raise LaunchplaneSafetyError("invalid_response")
        for rule_value in component_rules:
            rule = _require_dict(rule_value)
            if any(str(key) not in CHANGE_IMPACT_COMPONENT_RULE_FIELDS for key in rule):
                raise LaunchplaneSafetyError("unsafe_response_shape")
            product_impact = rule.get("product_impact")
            if product_impact is not None and product_impact != "declared_none":
                raise LaunchplaneSafetyError("invalid_response")
            _optional_bool(rule.get("governance_impact"))
            generated_by = rule.get("generated_by")
            if record.get("classification_model") != "v2" and any(
                rule.get(field) is not None
                for field in ("product_impact", "governance_impact", "generated_by")
            ):
                raise LaunchplaneSafetyError("invalid_response")
            if generated_by is not None:
                if (
                    not isinstance(generated_by, list) or not 1 <= len(generated_by) <= 20
                    or any(not isinstance(item, str) or not item.strip() for item in generated_by)
                ):
                    raise LaunchplaneSafetyError("invalid_response")
                if len(set(generated_by)) != len(generated_by) or product_impact is not None or rule.get("affected_products"):
                    raise LaunchplaneSafetyError("invalid_response")
            if product_impact is not None and rule.get("affected_products"):
                raise LaunchplaneSafetyError("invalid_response")
            path_prefixes = rule.get("path_prefixes")
            if path_prefixes is not None and not isinstance(path_prefixes, list):
                raise LaunchplaneSafetyError("invalid_response")
            affected_products = rule.get("affected_products")
            if affected_products is None:
                continue
            if not isinstance(affected_products, list):
                raise LaunchplaneSafetyError("invalid_response")
            for scope_value in affected_products:
                scope = _require_dict(scope_value)
                if any(str(key) not in CHANGE_IMPACT_PRODUCT_SCOPE_FIELDS for key in scope):
                    raise LaunchplaneSafetyError("unsafe_response_shape")
    revision = record.get("policy_revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise LaunchplaneSafetyError("invalid_response")
    policy_status = public_code(record.get("status"))
    if policy_status not in {"active", "superseded"}:
        raise LaunchplaneSafetyError("invalid_response")
    policy_digest = public_identifier(record.get("policy_digest"))
    if len(policy_digest) != 64 or any(character not in "0123456789abcdef" for character in policy_digest):
        raise LaunchplaneSafetyError("invalid_response")
    projected = {
        "record_id": public_identifier(record.get("record_id")),
        "policy_digest": policy_digest,
        "policy_revision": revision,
        "status": policy_status,
        "effective_at": public_timestamp(record.get("effective_at")),
    }
    assert_public_safe_shape(projected)
    return projected


def _project_change_impact_policy_result(result: object) -> dict[str, object]:
    source = _require_dict(result)
    if any(str(key) not in CHANGE_IMPACT_POLICY_RESULT_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    apply_status = public_code(source.get("status"))
    if apply_status not in {"would_apply", "would_replay", "applied", "replayed"}:
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "status": apply_status,
        "record": _project_change_impact_policy_record(source.get("record")),
    }
    projected.update(_project_change_impact_attribution(source, projected["record"], apply_status=apply_status))
    return projected


def _validate_private_string(value: object, *, maximum: int, minimum: int = 0) -> None:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise LaunchplaneSafetyError("invalid_response")


def _project_change_impact_attribution(
    source: dict[str, Any], policy: object, *, apply_status: str | None = None,
) -> dict[str, object]:
    if "audit" not in source and "attribution_status" not in source:
        return {}
    status = source.get("attribution_status")
    audit_value = source.get("audit")
    allowed = {"attributed", "legacy_unattributed", "attribution_unavailable"}
    if apply_status in {"would_apply", "would_replay"}:
        allowed = {"not_applied"}
    elif apply_status is not None:
        allowed = {"attributed", "legacy_unattributed"}
    if not isinstance(status, str) or status not in allowed:
        raise LaunchplaneSafetyError("invalid_response")
    if (status == "attributed") != (audit_value is not None):
        raise LaunchplaneSafetyError("invalid_response")
    if policy is None and status != "attribution_unavailable":
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {"attribution_status": status, "audit": None}
    if audit_value is None:
        return projected
    record = _require_dict(policy)
    audit = _require_exact_fields(audit_value, CHANGE_IMPACT_POLICY_AUDIT_FIELDS)
    if type(audit["schema_version"]) is not int or audit["schema_version"] != 1:
        raise LaunchplaneSafetyError("invalid_response")
    if audit["record_id"] != record["record_id"] or audit["policy_digest"] != record["policy_digest"]:
        raise LaunchplaneSafetyError("invalid_response")
    kind = audit["actor_kind"]
    if not isinstance(kind, str) or kind not in {"local_admin", "local_operator", "github_actions"}:
        raise LaunchplaneSafetyError("invalid_response")
    _validate_private_string(audit["actor_subject"], minimum=1, maximum=512)
    if not audit["actor_subject"].strip():
        raise LaunchplaneSafetyError("invalid_response")
    _validate_private_string(audit["trace_id"], minimum=1, maximum=256)
    workflow_value = audit["workflow_identity"]
    if (kind == "github_actions") != (workflow_value is not None):
        raise LaunchplaneSafetyError("invalid_response")
    if workflow_value is not None:
        workflow = _require_exact_fields(workflow_value, CHANGE_IMPACT_POLICY_WORKFLOW_FIELDS)
        for field, minimum, maximum in (
            ("repository", 1, 256), ("repository_id", 0, 64), ("repository_owner_id", 0, 64),
            ("workflow_ref", 0, 512), ("job_workflow_ref", 0, 512), ("ref", 0, 512), ("sha", 1, 64),
        ):
            _validate_private_string(workflow[field], minimum=minimum, maximum=maximum)
    recorded_at = audit["recorded_at"]
    if not isinstance(recorded_at, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", recorded_at,
    ):
        raise LaunchplaneSafetyError("invalid_response")
    try:
        recorded_display = datetime.fromisoformat(recorded_at).astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise LaunchplaneSafetyError("invalid_response") from exc
    recorded_display_text = recorded_display.isoformat(timespec="seconds").replace("+00:00", "Z")
    projected["audit"] = {
        "record_id": record["record_id"], "policy_digest": record["policy_digest"],
        "actor_kind": kind, "recorded_at": public_timestamp(recorded_display_text),
    }
    assert_public_safe_shape(projected)
    return projected


def _project_sha256(value: object) -> str:
    digest = public_identifier(value)
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise LaunchplaneSafetyError("invalid_response")
    return digest


def _project_merge_train_policy_summary(value: object) -> dict[str, object]:
    source = _require_exact_fields(value, MERGE_TRAIN_POLICY_SUMMARY_FIELDS)
    projected = {
        "record_id": public_identifier(source.get("record_id")),
        "updated_at": public_timestamp(source.get("updated_at")),
        "policy_sha256": _project_sha256(source.get("policy_sha256")),
    }
    assert_public_safe_shape(projected)
    return projected


def _project_merge_train_policy_targets(value: object) -> dict[str, object]:
    source = _require_exact_fields(value, MERGE_TRAIN_POLICY_TARGETS_FIELDS)
    if public_code(source.get("status")) != "ok":
        raise LaunchplaneSafetyError("invalid_response")
    targets = source.get("targets")
    if not isinstance(targets, list):
        raise LaunchplaneSafetyError("invalid_response")
    for target_value in targets:
        target = _require_exact_fields(target_value, MERGE_TRAIN_POLICY_TARGET_FIELDS)
        public_identifier(target.get("repository"))
        public_identifier(target.get("base_branch"))
        public_identifier(target.get("policy_key"))
        scheduler = _require_exact_fields(
            target.get("scheduler"), MERGE_TRAIN_SCHEDULER_FIELDS
        )
        if not isinstance(scheduler.get("enabled"), bool) or not isinstance(
            scheduler.get("mutate"), bool
        ):
            raise LaunchplaneSafetyError("invalid_response")
        public_code(scheduler.get("runner_mode"))
        service_authz = _require_exact_fields(
            target.get("service_authz"), MERGE_TRAIN_SERVICE_AUTHZ_FIELDS
        )
        public_identifier(service_authz.get("action"))
        public_identifier(service_authz.get("product"))
        public_identifier(service_authz.get("context"))
    projected = {
        **_project_merge_train_policy_summary(source.get("policy")),
        "target_count": len(targets),
        "trace_id": public_trace_id(source.get("trace_id")),
    }
    assert_public_safe_shape(projected)
    return projected


def _project_merge_train_policy_import_response(
    provider_payload: dict[str, Any], *, operation: str
) -> dict[str, object]:
    if any(str(key) not in SUCCESS_TOP_LEVEL_KEYS for key in provider_payload):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    if public_code(provider_payload.get("status")) != "accepted":
        raise LaunchplaneSafetyError("invalid_response")
    _project_records(provider_payload.get("records"), set())
    replayed = provider_payload.get("replayed")
    if replayed is not None and not isinstance(replayed, bool):
        raise LaunchplaneSafetyError("invalid_response")
    if provider_payload.get("original_trace_id"):
        public_trace_id(provider_payload.get("original_trace_id"))
    result = _require_exact_fields(
        provider_payload.get("result"), MERGE_TRAIN_POLICY_IMPORT_RESULT_FIELDS
    )
    expected_mode = (
        "dry_run" if operation == "merge-train-policy-import-dry-run" else "apply"
    )
    if public_code(result.get("mode")) != expected_mode:
        raise LaunchplaneSafetyError("invalid_response")
    record = _require_exact_fields(
        result.get("record"), MERGE_TRAIN_POLICY_IMPORT_RECORD_FIELDS
    )
    status = public_code(record.get("status"))
    if status not in {"active", "superseded"}:
        raise LaunchplaneSafetyError("invalid_response")
    repository_count = record.get("repository_count")
    policy_keys = record.get("policy_keys")
    if (
        not isinstance(repository_count, int)
        or isinstance(repository_count, bool)
        or repository_count < 1
        or not isinstance(policy_keys, list)
        or len(policy_keys) != repository_count
    ):
        raise LaunchplaneSafetyError("invalid_response")
    for policy_key in policy_keys:
        public_identifier(policy_key)
    public_identifier(record.get("source"))
    projected = {
        "record_id": public_identifier(record.get("record_id")),
        "status": status,
        "updated_at": public_timestamp(record.get("updated_at")),
        "policy_sha256": _project_sha256(record.get("policy_sha256")),
        "target_count": repository_count,
    }
    if replayed is not None:
        projected["replayed"] = replayed
    assert_public_safe_shape(projected)
    return projected


def summarize_merge_train_policy_import_success(
    *,
    operation: str,
    request: dict[str, object],
    current_policy: dict[str, object],
    provider_payload: dict[str, Any],
    expected_record_id: str,
    expected_policy_sha256: str,
) -> dict[str, object]:
    candidate = _project_merge_train_policy_import_response(
        provider_payload, operation=operation
    )
    if (
        candidate["record_id"] != expected_record_id
        or candidate["policy_sha256"] != expected_policy_sha256
    ):
        raise LaunchplaneSafetyError("invalid_response")
    payload = base_payload(
        status="accepted", operation=operation, request=request
    )
    payload["result"] = {
        "mode": "dry_run"
        if operation == "merge-train-policy-import-dry-run"
        else "apply",
        "current_policy": current_policy,
        "candidate": candidate,
    }
    payload["summary"] = {
        "launchplane_status": "accepted",
        "trace_id": public_trace_id(provider_payload.get("trace_id")),
        "current_policy_sha256": current_policy["policy_sha256"],
        "candidate_policy_sha256": candidate["policy_sha256"],
        "recommendation": (
            "Review and retain this redacted evidence before applying the exact private payload."
            if operation == "merge-train-policy-import-dry-run"
            else "Read back the active merge-train policy before relying on this mutation."
        ),
    }
    assert_public_safe_shape(payload["result"])
    assert_public_safe_shape(payload["summary"])
    return payload


def _project_generic_web_deploy_recovery_result(
    result: object, *, operation: str
) -> dict[str, object]:
    source = _require_dict(result)
    dry_run = operation == "generic-web-deploy-recovery-dry-run"
    allowed_fields = (
        GENERIC_WEB_DEPLOY_RECOVERY_DRY_RUN_FIELDS
        if dry_run
        else GENERIC_WEB_DEPLOY_RECOVERY_APPLY_FIELDS
    )
    if any(str(key) not in allowed_fields for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    expected_status = "ok" if dry_run else "accepted"
    expected_mode = "dry-run" if dry_run else "apply"
    status = public_code(source.get("status"))
    mode = public_code(source.get("mode"))
    if status != expected_status or mode != expected_mode or source.get("schema_version") != 1:
        raise LaunchplaneSafetyError("invalid_response")
    reservation_attempt = source.get("reservation_attempt")
    retry_safe = source.get("retry_safe")
    if (
        not isinstance(reservation_attempt, int)
        or isinstance(reservation_attempt, bool)
        or reservation_attempt < 1
        or not isinstance(retry_safe, bool)
    ):
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "status": status,
        "mode": mode,
        "product": public_identifier(source.get("product")),
        "context": public_identifier(source.get("context")),
        "instance": public_identifier(source.get("instance")),
        "reservation_state": public_code(source.get("reservation_state")),
        "reservation_attempt": reservation_attempt,
        "provider_outcome": public_code(source.get("provider_outcome")),
        "retry_safe": retry_safe,
        "recovery_digest": _project_sha256(source.get("recovery_digest")),
    }
    provider_status = source.get("provider_status")
    if provider_status:
        projected["provider_status"] = public_summary_string(provider_status)
    if dry_run:
        projected.update(
            {
                "reservation_created_at": public_timestamp(
                    source.get("reservation_created_at")
                ),
                "reservation_updated_at": public_timestamp(
                    source.get("reservation_updated_at")
                ),
                "observed_at": public_timestamp(source.get("observed_at")),
                "reconciliation_key_sha256": _project_sha256(
                    source.get("reconciliation_key_sha256")
                ),
                "provider_target_key_sha256": _project_sha256(
                    source.get("provider_target_key_sha256")
                ),
                "proposed_action": public_code(source.get("proposed_action")),
            }
        )
        provider_effect_phase = source.get("provider_effect_phase")
        if provider_effect_phase:
            projected["provider_effect_phase"] = public_code(provider_effect_phase)
        lease_expires_at = source.get("reservation_lease_expires_at")
        if lease_expires_at:
            projected["reservation_lease_expires_at"] = public_timestamp(lease_expires_at)
    else:
        projected["trace_id"] = public_trace_id(source.get("trace_id"))
        projected["recovery_action"] = public_code(source.get("recovery_action"))
    assert_public_safe_shape(projected)
    return projected


def _project_repository_inventory_record(value: object) -> dict[str, object]:
    source = _require_dict(value)
    if any(str(key) not in REPOSITORY_INVENTORY_RECORD_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    inventory_state = public_code(source.get("inventory_state"))
    if inventory_state not in {"tracked", "retired"}:
        raise LaunchplaneSafetyError("invalid_response")
    inventory_revision = source.get("inventory_revision")
    if (
        not isinstance(inventory_revision, int)
        or isinstance(inventory_revision, bool)
        or inventory_revision < 1
    ):
        raise LaunchplaneSafetyError("invalid_response")
    supersedes_record_id = source.get("supersedes_record_id")
    if supersedes_record_id is not None:
        supersedes_record_id = public_identifier(supersedes_record_id)
    projected = {
        "record_id": public_identifier(source.get("record_id")),
        "inventory_state": inventory_state,
        "inventory_revision": inventory_revision,
        "recorded_at": public_timestamp(source.get("recorded_at")),
        "supersedes_record_id": supersedes_record_id,
        "inventory_digest": _project_sha256(source.get("inventory_digest")),
    }
    assert_public_safe_shape(projected)
    return projected


def _project_repository_inventory_read_model(value: object) -> dict[str, object]:
    source = _require_dict(value)
    if any(str(key) not in REPOSITORY_INVENTORY_READ_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    status = public_code(source.get("status"))
    if status not in {"available", "missing", "ambiguous"}:
        raise LaunchplaneSafetyError("invalid_response")
    history_count = source.get("history_count")
    if not isinstance(history_count, int) or isinstance(history_count, bool) or history_count < 0:
        raise LaunchplaneSafetyError("invalid_response")
    current_record = source.get("current_record")
    if (status == "available") != (current_record is not None):
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "status": status,
        "history_count": history_count,
        "generated_at": public_timestamp(source.get("generated_at")),
        "current_record": None,
    }
    if current_record is not None:
        projected["current_record"] = _project_repository_inventory_record(current_record)
    assert_public_safe_shape(projected)
    return projected


def _project_repository_inventory_apply_result(value: object) -> dict[str, object]:
    source = _require_dict(value)
    if any(str(key) not in REPOSITORY_INVENTORY_APPLY_RESULT_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    status = public_code(source.get("status"))
    if status not in {"would_apply", "applied"}:
        raise LaunchplaneSafetyError("invalid_response")
    mode = public_code(source.get("mode"))
    if mode not in {"dry_run", "apply"}:
        raise LaunchplaneSafetyError("invalid_response")
    if (status, mode) not in {("would_apply", "dry_run"), ("applied", "apply")}:
        raise LaunchplaneSafetyError("invalid_response")
    inventory_revision = source.get("inventory_revision")
    if (
        not isinstance(inventory_revision, int)
        or isinstance(inventory_revision, bool)
        or inventory_revision < 1
    ):
        raise LaunchplaneSafetyError("invalid_response")
    supersedes_record_id = source.get("supersedes_record_id")
    if supersedes_record_id is not None:
        supersedes_record_id = public_identifier(supersedes_record_id)
    projected = {
        "status": status,
        "mode": mode,
        "inventory_revision": inventory_revision,
        "record_id": public_identifier(source.get("record_id")),
        "inventory_digest": _project_sha256(source.get("inventory_digest")),
        "supersedes_record_id": supersedes_record_id,
        "applied_at": public_timestamp(source.get("applied_at")),
    }
    assert_public_safe_shape(projected)
    return projected


def _project_change_impact_policy_read_model(value: object) -> dict[str, object]:
    source = _require_dict(value)
    if any(str(key) not in CHANGE_IMPACT_POLICY_READ_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    history_count = source.get("policy_history_count")
    if not isinstance(history_count, int) or isinstance(history_count, bool) or history_count < 0:
        raise LaunchplaneSafetyError("invalid_response")
    current_policy = source.get("current_policy")
    projected: dict[str, object] = {
        "policy_history_count": history_count,
        "current_policy": None,
    }
    # Older service responses may include these flags; current read models do not.
    for field in ("mode", "enforcement_effect"):
        if source.get(field) is not None:
            if not isinstance(source[field], str):
                raise LaunchplaneSafetyError("invalid_response")
            projected[field] = public_code(source[field])
    if source.get("authoritative") is not None:
        projected["authoritative"] = _optional_bool(source["authoritative"])
    if current_policy is not None:
        projected["current_policy"] = _project_change_impact_policy_record(current_policy)
    projected.update(_project_change_impact_attribution(source, projected["current_policy"]))
    assert_public_safe_shape(projected)
    return projected


ODOO_ADDON_SETTINGS_RESULT_FIELDS = {
    "status",
    "mode",
    "product",
    "context",
    "instance",
    "addon",
    "production_lane",
    "record_exists",
    "changed",
    "applied",
    "rendered_action",
    "changes",
    "read_back",
    "read_back_matches",
    "reason",
    "source_label",
    "record_sha256_before",
    "record_sha256_after",
    "plan_sha256",
    "next_actions",
}
ODOO_ADDON_SETTING_EVIDENCE_FIELDS = {
    "setting",
    "source",
    "value",
    "value_present",
    "secret_binding_id",
    "secret_binding_present",
}
# Only these settings may show a literal value. Everything else shows presence only.
ODOO_ADDON_SETTINGS_NON_SECRET_SETTINGS = {
    "shop_url_key",
    "api_version",
    "test_store",
    "allow_production",
    "production_indicators",
}
ODOO_ADDON_SETTINGS_PAYLOAD_FIELDS = {
    "schema_version",
    "product",
    "context",
    "instance",
    "addon",
    "reason",
    "shopify",
}
ODOO_ADDON_SETTINGS_SHOPIFY_FIELDS = {
    "shop_url_key",
    "api_version",
    "api_token_secret_binding_id",
    "webhook_key_secret_binding_id",
    "test_store",
}


INTEGRATION_ALLOWANCE_FIELDS = {
    "integration",
    "kind",
    "reason",
    "evidence",
    "recorded_by",
    "recorded_at",
}
INTEGRATION_ALLOWANCE_KINDS = {"dev_store", "read_only_source", "pre_live"}
# Why a production integration key may sit on a non-production lane: the
# allowance kinds plus a key the site's stable lanes share on purpose.
SECRET_SHARING_REASON_KINDS = INTEGRATION_ALLOWANCE_KINDS | {"site_shared"}
SECRET_SHARING_REASON_FIELDS = {"kind", "reason", "evidence", "recorded_by", "recorded_at"}
LANE_INTEGRATION_KEY_FIELDS = {"binding_key", "declared_secret_class", "sharing_reason"}
# A long run of letters and digits in person-written text, such as a pasted API
# key (rk_live_..., a hex token): redacted before a sharing reason is shown.
CREDENTIAL_LIKE_WORD_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]*[0-9])(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{20,}"
)
INTEGRATION_ALLOWANCES_PLAN_FIELDS = {
    "status",
    "mode",
    "product",
    "context",
    "instance",
    "environment_class",
    "changed",
    "applied",
    "changes",
    "read_back",
    "read_back_matches",
    "reason",
    "source_label",
    "record_sha256_before",
    "record_sha256_after",
    "plan_sha256",
}
INTEGRATION_ALLOWANCES_READ_FIELDS = {
    "status",
    "product",
    "context",
    "instance",
    "environment_class",
    "allowances",
    "integration_keys",
    "record_sha256",
}
INTEGRATION_ALLOWANCES_PAYLOAD_FIELDS = {
    "schema_version",
    "product",
    "context",
    "instance",
    "reason",
    "allowances",
}
INTEGRATION_ALLOWANCE_INPUT_FIELDS = {"integration", "kind", "reason", "evidence"}
TESTING_HOLD_FIELDS = {"reason", "recorded_by", "recorded_at"}
TESTING_HOLD_ACTIONS = {"set", "update", "clear", "unchanged"}
TESTING_HOLD_PLAN_FIELDS = {
    "status",
    "mode",
    "product",
    "context",
    "instance",
    "action",
    "changed",
    "applied",
    "before",
    "after",
    "read_back",
    "read_back_matches",
    "reconcile_requested",
    "reason",
    "source_label",
    "record_sha256_before",
    "record_sha256_after",
    "plan_sha256",
}
TESTING_HOLD_READ_FIELDS = {
    "status",
    "product",
    "context",
    "instance",
    "hold",
    "record_sha256",
}
PRODUCT_REPOSITORY_IDENTITY_FIELDS = {"repository_id", "repository_owner_id"}
PRODUCT_REPOSITORY_IDENTITY_OPERATIONS = {"record", "unchanged"}
PRODUCT_REPOSITORY_IDENTITY_PLAN_FIELDS = {
    "status",
    "mode",
    "product",
    "repository",
    "operation",
    "identity_before",
    "identity_after",
    "inventory_record_id",
    "inventory_revision",
    "inventory_digest",
    "changed",
    "applied",
    "reason",
    "source_label",
    "profile_record_sha256_before",
    "profile_updated_at_before",
    "profile_updated_at_after",
    "plan_sha256",
    "read_back",
    "read_back_matches",
}
REVIEWED_PLAN_MODES = {"dry-run", "apply"}


def _optional_sha256(value: object) -> str:
    if value in {None, ""}:
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise LaunchplaneSafetyError("invalid_response")
    return value


def _project_odoo_addon_setting_evidence(value: object) -> dict[str, object]:
    source = _require_dict(value)
    if any(str(key) not in ODOO_ADDON_SETTING_EVIDENCE_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    setting = public_code(source.get("setting"))
    value_source = source.get("source")
    if value_source not in {"literal", "secret_binding"}:
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "setting": setting,
        "source": value_source,
        "present": bool(source.get("value_present")),
    }
    if value_source == "secret_binding":
        if source.get("value") is not None:
            raise LaunchplaneSafetyError("unsafe_response_shape")
        projected["binding_ref"] = public_identifier(source.get("secret_binding_id"))
        binding_present = _optional_bool(source.get("secret_binding_present"))
        if binding_present is not None:
            projected["binding_present"] = binding_present
        return projected
    literal = source.get("value")
    if literal is None:
        return projected
    if setting not in ODOO_ADDON_SETTINGS_NON_SECRET_SETTINGS:
        raise LaunchplaneSafetyError("unsafe_response_shape")
    projected["literal"] = literal if isinstance(literal, bool) else public_identifier(literal)
    return projected


def _project_integration_allowance(value: object) -> dict[str, object]:
    source = _require_dict(value)
    if any(str(key) not in INTEGRATION_ALLOWANCE_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    kind = source.get("kind")
    if kind not in INTEGRATION_ALLOWANCE_KINDS:
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "integration": public_code(source.get("integration")),
        "kind": kind,
        "reason": public_summary_string(source.get("reason")),
    }
    if source.get("evidence"):
        projected["evidence"] = public_summary_string(source.get("evidence"))
    if source.get("recorded_by"):
        projected["recorded_by"] = public_identifier(source.get("recorded_by"))
    if source.get("recorded_at"):
        projected["recorded_at"] = public_summary_string(source.get("recorded_at"), max_length=64)
    return projected


def _redact_credential_like_words(text: str) -> str:
    return CREDENTIAL_LIKE_WORD_RE.sub("[redacted]", text)


def _project_sharing_reason(value: object) -> dict[str, object]:
    """Why a key is shared, as recorded by a person; metadata, never the value."""
    source = _require_dict(value)
    if any(str(key) not in SECRET_SHARING_REASON_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    kind = source.get("kind")
    if kind not in SECRET_SHARING_REASON_KINDS:
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "kind": kind,
        "reason": _redact_credential_like_words(public_summary_string(source.get("reason"))),
        "evidence": _redact_credential_like_words(public_summary_string(source.get("evidence"))),
    }
    if source.get("recorded_by"):
        projected["recorded_by"] = public_identifier(source.get("recorded_by"))
    if source.get("recorded_at"):
        projected["recorded_at"] = public_summary_string(source.get("recorded_at"), max_length=64)
    return projected


def _project_lane_integration_keys(value: object) -> list[dict[str, object]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LaunchplaneSafetyError("invalid_response")
    projected: list[dict[str, object]] = []
    for item in value:
        source = _require_dict(item)
        if any(str(key) not in LANE_INTEGRATION_KEY_FIELDS for key in source):
            raise LaunchplaneSafetyError("unsafe_response_shape")
        key: dict[str, object] = {"binding_key": public_identifier(source.get("binding_key"))}
        # Projected as secret_class: the public-safety shape check refuses other
        # key names containing "secret".
        if source.get("declared_secret_class"):
            key["secret_class"] = public_code(source.get("declared_secret_class"))
        if source.get("sharing_reason") is not None:
            key["sharing_reason"] = _project_sharing_reason(source.get("sharing_reason"))
        projected.append(key)
    return projected


def _project_integration_allowance_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise LaunchplaneSafetyError("invalid_response")
    return [_project_integration_allowance(item) for item in value]


def _project_integration_allowances_plan(result: object) -> dict[str, object]:
    source = _require_dict(result)
    if any(str(key) not in INTEGRATION_ALLOWANCES_PLAN_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    projected: dict[str, object] = {
        "status": public_code(source.get("status")),
        "mode": public_code(source.get("mode")),
        "product": public_identifier(source.get("product")),
        "context": public_identifier(source.get("context")),
        "instance": public_identifier(source.get("instance")),
        "environment_class": public_code(source.get("environment_class")),
        "reason": public_summary_string(source.get("reason")),
        "source_label": public_identifier(source.get("source_label")),
        "record_sha256_before": _optional_sha256(source.get("record_sha256_before")),
        "record_sha256_after": _optional_sha256(source.get("record_sha256_after")),
        "plan_sha256": _optional_sha256(source.get("plan_sha256")),
        "changed": bool(_optional_bool(source.get("changed"))),
        "applied": bool(_optional_bool(source.get("applied"))),
    }
    read_back_matches = _optional_bool(source.get("read_back_matches"))
    if read_back_matches is not None:
        projected["read_back_matches"] = read_back_matches
    changes = source.get("changes", [])
    if not isinstance(changes, list):
        raise LaunchplaneSafetyError("invalid_response")
    projected_changes: list[dict[str, object]] = []
    for change_value in changes:
        change = _require_dict(change_value)
        if any(str(key) not in {"integration", "action", "before", "after"} for key in change):
            raise LaunchplaneSafetyError("unsafe_response_shape")
        action = change.get("action")
        if action not in {"add", "update", "remove", "unchanged"}:
            raise LaunchplaneSafetyError("invalid_response")
        projected_change: dict[str, object] = {
            "integration": public_code(change.get("integration")),
            "action": action,
        }
        for side in ("before", "after"):
            if change.get(side) is not None:
                projected_change[side] = _project_integration_allowance(change[side])
        projected_changes.append(projected_change)
    projected["changes"] = projected_changes
    projected["read_back"] = _project_integration_allowance_list(source.get("read_back", []))
    assert_public_safe_shape(projected)
    return projected


def _project_integration_allowances_read(result: object) -> dict[str, object]:
    source = _require_dict(result)
    if any(str(key) not in INTEGRATION_ALLOWANCES_READ_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    projected: dict[str, object] = {
        "status": public_code(source.get("status")),
        "product": public_identifier(source.get("product")),
        "context": public_identifier(source.get("context")),
        "instance": public_identifier(source.get("instance")),
        "environment_class": public_code(source.get("environment_class")),
        "allowances": _project_integration_allowance_list(source.get("allowances")),
        "integration_keys": _project_lane_integration_keys(source.get("integration_keys")),
        "record_sha256": _optional_sha256(source.get("record_sha256")),
    }
    assert_public_safe_shape(projected)
    return projected


def _reviewed_plan_mode(value: object) -> str:
    if value not in REVIEWED_PLAN_MODES:
        raise LaunchplaneSafetyError("invalid_response")
    return cast(str, value)


def _project_testing_hold(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    source = _require_dict(value)
    if any(str(key) not in TESTING_HOLD_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    projected: dict[str, object] = {"reason": public_summary_string(source.get("reason"))}
    if source.get("recorded_by"):
        projected["recorded_by"] = public_identifier(source.get("recorded_by"))
    if source.get("recorded_at"):
        projected["recorded_at"] = public_summary_string(source.get("recorded_at"), max_length=64)
    return projected


def _project_testing_hold_plan(result: object) -> dict[str, object]:
    source = _require_dict(result)
    if any(str(key) not in TESTING_HOLD_PLAN_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    action = source.get("action")
    if action not in TESTING_HOLD_ACTIONS:
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "status": public_code(source.get("status")),
        "mode": _reviewed_plan_mode(source.get("mode")),
        "product": public_identifier(source.get("product")),
        "context": public_identifier(source.get("context")),
        "instance": public_identifier(source.get("instance")),
        "action": action,
        "before": _project_testing_hold(source.get("before")),
        "after": _project_testing_hold(source.get("after")),
        "read_back": _project_testing_hold(source.get("read_back")),
        "reason": public_summary_string(source.get("reason")),
        "source_label": public_identifier(source.get("source_label")),
        "record_sha256_before": _optional_sha256(source.get("record_sha256_before")),
        "record_sha256_after": _optional_sha256(source.get("record_sha256_after")),
        "plan_sha256": _optional_sha256(source.get("plan_sha256")),
        "changed": bool(_optional_bool(source.get("changed"))),
        "applied": bool(_optional_bool(source.get("applied"))),
        "reconcile_requested": bool(_optional_bool(source.get("reconcile_requested"))),
    }
    read_back_matches = _optional_bool(source.get("read_back_matches"))
    if read_back_matches is not None:
        projected["read_back_matches"] = read_back_matches
    assert_public_safe_shape(projected)
    return projected


def _project_testing_hold_read(result: object) -> dict[str, object]:
    source = _require_dict(result)
    if any(str(key) not in TESTING_HOLD_READ_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    projected: dict[str, object] = {
        "status": public_code(source.get("status")),
        "product": public_identifier(source.get("product")),
        "context": public_identifier(source.get("context")),
        "instance": public_identifier(source.get("instance")),
        "hold": _project_testing_hold(source.get("hold")),
        "record_sha256": _optional_sha256(source.get("record_sha256")),
    }
    assert_public_safe_shape(projected)
    return projected


PRODUCT_READ_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PRODUCT_ACTIVITY_MAX_EVENTS = 50
PRODUCT_ACTIVITY_MAX_RECORD_LINKS = 10
PRODUCT_HEALTH_MAX_CHECKS = 20
PRODUCT_ACTIVITY_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,79}$")
PREVIEW_MAX_GENERATIONS = 20
PREVIEW_MAX_SOURCES = 10
RECONCILE_MAX_REQUESTS = 50
RECONCILE_MAX_PLAN_LIST_ITEMS = 20


def _product_read_path(command: str, **segments: str) -> str:
    """The local-extension route with each path segment validated and percent-encoded."""
    encoded: dict[str, str] = {}
    for name, value in segments.items():
        if not isinstance(value, str) or not PRODUCT_READ_PATH_SEGMENT_RE.fullmatch(value):
            raise ValueError(f"invalid_{name}")
        encoded[name] = urllib.parse.quote(value, safe="")
    return helper_command_path(command).format(**encoded)


def _optional_text(value: object, *, max_length: int = 64) -> str:
    if value in {None, ""}:
        return ""
    return public_summary_string(value, max_length=max_length)


def _optional_code(value: object) -> str:
    if value in {None, ""}:
        return ""
    return public_code(value)


def _optional_identifier(value: object) -> str:
    if value in {None, ""}:
        return ""
    return public_identifier(value)


def _optional_dict(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    return _require_dict(value)


def _project_product_provenance(value: object) -> dict[str, object] | None:
    source = _optional_dict(value)
    if source is None:
        return None
    return {
        "source_kind": _optional_code(source.get("source_kind")),
        "source_record_id": _optional_identifier(source.get("source_record_id")),
        "recorded_at": _optional_text(source.get("recorded_at")),
        "refreshed_at": _optional_text(source.get("refreshed_at")),
        "freshness_status": _optional_code(source.get("freshness_status")),
    }


def _project_product_runtime_identity(value: object) -> dict[str, object] | None:
    source = _optional_dict(value)
    if source is None:
        return None
    return {
        "context": _optional_identifier(source.get("context")),
        "instance": _optional_identifier(source.get("instance")),
        "environment_kind": _optional_code(source.get("environment_kind")),
        "deployment_record_id": _optional_identifier(source.get("deployment_record_id")),
        "artifact_id": _optional_identifier(source.get("artifact_id")),
        "source_git_ref": _optional_identifier(source.get("source_git_ref")),
        "image_reference": _optional_identifier(source.get("image_reference")),
        "release_tuple_id": _optional_identifier(source.get("release_tuple_id")),
        "deployed_at": _optional_text(source.get("deployed_at")),
    }


def _project_product_artifact(value: object) -> dict[str, object] | None:
    source = _optional_dict(value)
    if source is None:
        return None
    image = _optional_dict(source.get("image")) or {}
    projected: dict[str, object] = {
        "artifact_id": _optional_identifier(source.get("artifact_id")),
        "source_commit": _optional_identifier(source.get("source_commit")),
        "image_repository": _optional_identifier(image.get("repository")),
        "image_digest": _optional_identifier(image.get("digest")),
    }
    build = _optional_dict(source.get("source_build"))
    if build is not None:
        run_id = build.get("run_id")
        pull_request_number = build.get("pull_request_number")
        for number in (run_id, pull_request_number):
            if number is not None and (isinstance(number, bool) or not isinstance(number, int)):
                raise LaunchplaneSafetyError("invalid_response")
        projected["source_build"] = {
            "repository": _optional_identifier(build.get("repository")),
            "event": _optional_code(build.get("event")),
            "purpose": _optional_code(build.get("purpose")),
            "run_id": run_id,
            "pull_request_number": pull_request_number,
        }
    return projected


def _project_product_target(value: object) -> dict[str, object]:
    source = _optional_dict(value) or {}
    return {
        "provider": _optional_code(source.get("provider")),
        "target_type": _optional_code(source.get("target_type")),
        "provider_target_type": _optional_code(source.get("provider_target_type")),
        "target_id_recorded": bool(_optional_bool(source.get("target_id_recorded"))),
        "artifact": _project_product_artifact(source.get("artifact_manifest")),
        "expected_runtime_identity": _project_product_runtime_identity(
            source.get("expected_runtime_identity")
        ),
        "observed_runtime_identity": _project_product_runtime_identity(
            source.get("observed_runtime_identity")
        ),
        "runtime_identity_status": _optional_code(source.get("runtime_identity_status")),
        "runtime_identity_detail": _optional_text(
            source.get("runtime_identity_detail"), max_length=300
        ),
        "trust_state": _optional_code(source.get("trust_state")),
    }


def _project_product_health(value: object) -> dict[str, object] | None:
    source = _optional_dict(value)
    if source is None:
        return None
    checks = source.get("checks") or []
    if not isinstance(checks, list):
        raise LaunchplaneSafetyError("invalid_response")
    projected_checks: list[dict[str, object]] = []
    for check_value in checks[:PRODUCT_HEALTH_MAX_CHECKS]:
        check = _require_dict(check_value)
        projected_checks.append(
            {
                "name": _optional_text(check.get("name"), max_length=120),
                "kind": _optional_code(check.get("kind")),
                "enabled": bool(_optional_bool(check.get("enabled"))),
                "status": _optional_code(check.get("status")),
                "failure_code": _optional_code(check.get("failure_code")),
                "observed_at": _optional_text(check.get("observed_at")),
                "incident_status": _optional_code(check.get("incident_status")),
                "trust_state": _optional_code(check.get("trust_state")),
            }
        )
    return {
        "monitoring_intent": _optional_code(source.get("monitoring_intent")),
        "trust_state": _optional_code(source.get("trust_state")),
        "checks": projected_checks,
        "checks_truncated": len(checks) > PRODUCT_HEALTH_MAX_CHECKS,
    }


def _project_product_public_ingress(value: object) -> dict[str, object] | None:
    source = _optional_dict(value)
    if source is None:
        return None
    return {
        "status": _optional_code(source.get("status")),
        "failure_code": _optional_code(source.get("failure_code")),
        "observed_at": _optional_text(source.get("observed_at")),
        "incident_status": _optional_code(source.get("incident_status")),
        "trust_state": _optional_code(source.get("trust_state")),
    }


def _project_product_environment(value: object) -> dict[str, object]:
    """Deploy-verification fields only; settings, secrets, actions and URLs are dropped."""
    source = _require_dict(value)
    projected: dict[str, object] = {
        "product": public_identifier(source.get("product")),
        "environment": public_identifier(source.get("environment")),
        "context": public_identifier(source.get("context")),
        "repository": _optional_identifier(source.get("repository")),
        "driver_id": _optional_identifier(source.get("driver_id")),
        "target": _project_product_target(source.get("target")),
        "health_monitoring": _project_product_health(source.get("health_monitoring")),
        "public_ingress": _project_product_public_ingress(source.get("public_ingress")),
        "trust_state": _optional_code(source.get("trust_state")),
        "provenance": _project_product_provenance(source.get("provenance")),
    }
    assert_public_safe_shape(projected)
    return projected


PRODUCT_PROFILE_MAX_LANES = 20
PRODUCT_PRODUCTION_USES = {"unknown", "prelaunch", "live"}
GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


def _project_product_profile(value: object) -> dict[str, object]:
    """Who owns the product and how its production is classified; settings, secrets,
    images, URLs, workflows and expected configuration are dropped."""
    source = _require_dict(value)
    owner = {} if source.get("owner") is None else source.get("owner")
    lanes = [] if source.get("lanes") is None else source.get("lanes")
    if not isinstance(owner, dict) or not isinstance(lanes, list):
        raise LaunchplaneSafetyError("invalid_response")
    production_use = source.get("production_use") or "unknown"
    if production_use not in PRODUCT_PRODUCTION_USES:
        # Only prelaunch skips Owner review; an unknown class must not read as either.
        raise LaunchplaneSafetyError("invalid_response")
    owner_login = owner.get("github_login") or ""
    if owner_login and (
        not isinstance(owner_login, str) or not GITHUB_LOGIN_RE.fullmatch(owner_login)
    ):
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "product": public_identifier(source.get("product")),
        "display_name": _optional_text(source.get("display_name"), max_length=120),
        "driver_id": _optional_identifier(source.get("driver_id")),
        "repository": _optional_identifier(source.get("repository")),
        "production_use": production_use,
        "lifecycle_state": _optional_code(source.get("lifecycle_state")),
        "owner_github_login": owner_login,
        "owner_review_label": _optional_code(owner.get("review_label")),
        "lanes": [
            {
                "context": public_identifier(_require_dict(lane).get("context")),
                "instance": public_identifier(_require_dict(lane).get("instance")),
            }
            for lane in lanes[:PRODUCT_PROFILE_MAX_LANES]
        ],
        "lanes_truncated": len(lanes) > PRODUCT_PROFILE_MAX_LANES,
        "updated_at": _optional_text(source.get("updated_at")),
    }
    assert_public_safe_shape(projected)
    return projected


class _FieldDrops:
    """Field paths a tolerant projection dropped, so a partial read says what is missing."""

    def __init__(self) -> None:
        self.paths: set[str] = set()
        self.count = 0

    def keep(self, path: str, validate: Any, value: object) -> object:
        """The validated value, or "" when it fails; a secret-looking value still fails the read."""
        if value is None or value == "":
            return ""
        assert_public_safe_shape(value)
        try:
            return validate(value)
        except LaunchplaneSafetyError:
            self.drop(path)
            return ""

    def drop(self, path: str) -> None:
        self.paths.add(path)
        self.count += 1


def _public_dotted_code(value: object) -> str:
    if not isinstance(value, str) or not PRODUCT_ACTIVITY_CODE_RE.fullmatch(value):
        raise LaunchplaneSafetyError("invalid_response")
    return value


def _project_product_activity_event(
    event_value: object, drops: _FieldDrops
) -> dict[str, object] | None:
    """One event, or None when its identity is unusable; odd optional fields are dropped."""
    if not isinstance(event_value, dict):
        return None
    event = cast(dict[str, Any], event_value)
    event_id = drops.keep("events[].event_id", public_identifier, event.get("event_id"))
    event_type = drops.keep("events[].event_type", _public_dotted_code, event.get("event_type"))
    if not event_id or not event_type:
        return None
    links = event.get("records")
    if not isinstance(links, list):
        links = []
    projected_links: list[dict[str, object]] = []
    for link_value in links[:PRODUCT_ACTIVITY_MAX_RECORD_LINKS]:
        link = link_value if isinstance(link_value, dict) else {}
        record_type = drops.keep(
            "events[].records[].record_type", _public_dotted_code, link.get("record_type")
        )
        record_id = drops.keep(
            "events[].records[].record_id", public_identifier, link.get("record_id")
        )
        if record_type and record_id:
            projected_links.append({"record_type": record_type, "record_id": record_id})

    def text(field: str, max_length: int) -> object:
        return drops.keep(
            f"events[].{field}",
            lambda value: public_summary_string(value, max_length=max_length),
            event.get(field),
        )

    return {
        "event_id": event_id,
        "event_type": event_type,
        "context": drops.keep("events[].context", public_identifier, event.get("context")),
        "environment": drops.keep(
            "events[].environment", public_identifier, event.get("environment")
        ),
        "action_id": drops.keep("events[].action_id", _public_dotted_code, event.get("action_id")),
        "status": drops.keep("events[].status", _public_dotted_code, event.get("status")),
        "occurred_at": text("occurred_at", 64),
        "title": text("title", 200),
        "summary": text("summary", 300),
        "records": projected_links,
        "records_truncated": len(links) > PRODUCT_ACTIVITY_MAX_RECORD_LINKS,
        "trust_state": drops.keep(
            "events[].trust_state", _public_dotted_code, event.get("trust_state")
        ),
    }


def _project_product_activity(value: object) -> dict[str, object]:
    """Recent activity, bounded. Odd fields are dropped and unusable events omitted, with
    counts and field paths; only a secret-looking value fails the whole read."""
    source = _require_dict(value)
    events = source.get("events") or []
    if not isinstance(events, list):
        raise LaunchplaneSafetyError("invalid_response")
    drops = _FieldDrops()
    projected_events: list[dict[str, object]] = []
    omitted_event_count = 0
    for event_value in events[:PRODUCT_ACTIVITY_MAX_EVENTS]:
        projected_event = _project_product_activity_event(event_value, drops)
        if projected_event is None:
            omitted_event_count += 1
        else:
            projected_events.append(projected_event)
    projected: dict[str, object] = {
        "product": public_identifier(source.get("product")),
        "repository": drops.keep("repository", public_identifier, source.get("repository")),
        "driver_id": drops.keep("driver_id", public_identifier, source.get("driver_id")),
        "events": projected_events,
        "events_truncated": len(events) > PRODUCT_ACTIVITY_MAX_EVENTS,
        "omitted_event_count": omitted_event_count,
        "dropped_field_count": drops.count,
        "dropped_field_paths": sorted(drops.paths),
    }
    assert_public_safe_shape(projected)
    return projected


def _public_origin_url(value: object) -> str:
    """Only the scheme and host of a public https URL; a path or query could carry a
    credential."""
    try:
        parts = urllib.parse.urlsplit(public_url(value))
    except ValueError as error:
        raise LaunchplaneSafetyError("invalid_response") from error
    return f"{parts.scheme}://{parts.netloc}"


def _reconcile_plan_validators() -> dict[str, Any]:
    """The plan fields the reconciler writes, each with its validator; others are dropped."""
    fields: dict[str, Any] = dict.fromkeys(
        (
            "target",
            "action",
            "reason",
            "deferred",
            "current_state",
            "preview_operation_status",
            "preview_result_status",
            "queued_operation_status",
            "last_failed_error_code",
        ),
        _public_dotted_code,
    )
    fields.update(
        dict.fromkeys(
            (
                "context",
                "head_sha",
                "current_head_sha",
                "desired_commit",
                "desired_artifact_id",
                "current_artifact_id",
                "desired_image_digest",
                "current_image_digest",
                "current_preview_id",
                "preview_plan_id",
                "preview_operation_key",
                "preview_slug",
                "queued_operation_id",
                "active_operation_id",
                "deployed_operation_id",
                "last_failed_operation_id",
                "hold_recorded_by",
            ),
            public_identifier,
        )
    )
    fields.update(
        detail=lambda value: public_summary_string(value, max_length=400),
        # Launchplane's fixed description plus up to 32 validated env-key names
        # (launchplane#2717); the helper's own summary redaction still applies.
        last_failed_error_summary=lambda value: public_summary_string(value, max_length=1500),
        hold_reason=lambda value: public_summary_string(value, max_length=300),
        hold_recorded_at=lambda value: public_summary_string(value, max_length=64),
        preview_url=_public_origin_url,
    )
    return fields


RECONCILE_PLAN_VALIDATORS = _reconcile_plan_validators()
# Plan lists of setting key names (never values), by the name they are output under.
RECONCILE_PLAN_KEY_NAME_LISTS = {
    "omitted_integration_credential_keys": "omitted_integration_keys",
    "missing_keys": "missing_keys",
}


def _project_reconcile_plan(plan_value: object, drops: _FieldDrops) -> dict[str, object]:
    """The plan fields the reconciler is known to write. Any other field, and a value
    that does not validate, is dropped and counted."""
    plan = plan_value if isinstance(plan_value, dict) else {}
    projected: dict[str, object] = {}
    for key, value in plan.items():
        validate = RECONCILE_PLAN_VALIDATORS.get(key)
        if validate is not None:
            projected[key] = drops.keep(f"requests[].last_plan.{key}", validate, value)
        elif key in {"held", "owner_review_requested"}:
            projected[key] = value if isinstance(value, bool) else None
        elif key == "pull_request_number":
            projected[key] = value if type(value) is int else None
        elif key in RECONCILE_PLAN_KEY_NAME_LISTS and isinstance(value, list):
            # Key names only; the output name avoids the sensitive-key denylist.
            output_key = RECONCILE_PLAN_KEY_NAME_LISTS[key]
            items = [
                drops.keep(f"requests[].last_plan.{output_key}[]", public_identifier, item)
                for item in value[:RECONCILE_MAX_PLAN_LIST_ITEMS]
            ]
            projected[output_key] = [item for item in items if item]
        else:
            drops.drop("requests[].last_plan.<unlisted field>")
    return projected


def _project_reconcile_request(
    request_value: object, drops: _FieldDrops
) -> dict[str, object] | None:
    if not isinstance(request_value, dict):
        return None
    request = cast(dict[str, Any], request_value)
    target_key = drops.keep("requests[].target_key", public_identifier, request.get("target_key"))
    if not target_key:
        return None

    def field(name: str, validate: Any) -> object:
        return drops.keep(f"requests[].{name}", validate, request.get(name))

    def count(name: str) -> int | None:
        value = request.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            if value is not None:
                drops.drop(f"requests[].{name}")
            return None
        return value

    return {
        "target_key": target_key,
        "target_kind": field("target_kind", _public_dotted_code),
        "pull_request_number": count("pull_request_number"),
        "state": field("state", _public_dotted_code),
        "requested_at": field("requested_at", lambda value: public_summary_string(value, max_length=64)),
        "updated_at": field("updated_at", lambda value: public_summary_string(value, max_length=64)),
        "request_count": count("request_count"),
        "attempt": count("attempt"),
        "last_delivery_id": field("last_delivery_id", public_identifier),
        "last_error": field("last_error", lambda value: public_summary_string(value, max_length=400)),
        "last_plan": _project_reconcile_plan(request.get("last_plan"), drops),
    }


def _project_reconcile_requests(provider_payload: dict[str, Any]) -> dict[str, object]:
    """Each target's last reconcile decision, bounded. Odd fields are dropped and unusable
    requests omitted, with counts and field paths; a secret-looking value fails the read."""
    requests = provider_payload.get("requests") or []
    if not isinstance(requests, list):
        raise LaunchplaneSafetyError("invalid_response")
    drops = _FieldDrops()
    projected_requests: list[dict[str, object]] = []
    omitted_request_count = 0
    for request_value in requests[:RECONCILE_MAX_REQUESTS]:
        projected_request = _project_reconcile_request(request_value, drops)
        if projected_request is None:
            omitted_request_count += 1
        else:
            projected_requests.append(projected_request)
    projected: dict[str, object] = {
        "product": public_identifier(provider_payload.get("product")),
        "requests": projected_requests,
        "requests_truncated": len(requests) > RECONCILE_MAX_REQUESTS,
        "omitted_request_count": omitted_request_count,
        "dropped_field_count": drops.count,
        "dropped_field_paths": sorted(drops.paths),
    }
    assert_public_safe_shape(projected)
    return projected


PRODUCT_SECRET_BINDINGS_MAX = 200
# Metadata fields the service returns for one binding (launchplane#2810); it sends
# no value or ciphertext.
PRODUCT_SECRET_BINDING_FIELDS = frozenset(
    {
        "binding_key",
        "name",
        "scope",
        "context",
        "instance",
        "secret_class",
        "sharing_reason",
        "version_id",
    }
)
PRODUCT_SECRET_BINDING_SCOPES = {"context", "context_instance"}


def _product_secret_binding_scope(value: object) -> str:
    if not isinstance(value, str) or value not in PRODUCT_SECRET_BINDING_SCOPES:
        raise LaunchplaneSafetyError("invalid_response")
    return value


def _project_product_secret_binding(
    binding_value: object, drops: _FieldDrops
) -> dict[str, object] | None:
    """One binding's metadata, or None when its identity is unusable. A sensitive
    field name, such as a value or ciphertext, or a secret-looking value in any
    field, kept or dropped, fails the whole read."""
    if not isinstance(binding_value, dict):
        return None
    binding = binding_value
    assert_public_safe_shape(binding)

    def field(name: str, validate: Any) -> object:
        return drops.keep(f"bindings[].{name}", validate, binding.get(name))

    binding_key = field("binding_key", public_identifier)
    context = field("context", public_identifier)
    scope = field("scope", _product_secret_binding_scope)
    if not binding_key or not context or not scope:
        return None
    instance = field("instance", public_identifier)
    if (scope == "context_instance") != bool(instance):
        drops.drop("bindings[].instance")
        return None
    sharing_reason: object = None
    if binding.get("sharing_reason") is not None:
        sharing_reason = field("sharing_reason", _project_sharing_reason) or None
    _report_dropped_fields(
        binding,
        prefix="bindings[]",
        kept=set(PRODUCT_SECRET_BINDING_FIELDS),
        known=PRODUCT_SECRET_BINDING_FIELDS,
        drops=drops,
    )
    return {
        "binding_key": binding_key,
        "name": field("name", public_identifier),
        "scope": scope,
        "context": context,
        "instance": instance,
        # Projected as secret_class: the public-safety shape check refuses other
        # key names containing "secret".
        "secret_class": field("secret_class", public_code),
        "sharing_reason": sharing_reason,
        "version_id": field("version_id", public_identifier),
    }


def _project_product_secret_bindings(provider_payload: dict[str, Any]) -> dict[str, object]:
    """A product's runtime secret binding metadata, bounded. Odd fields are dropped and
    unusable bindings omitted, with counts and field paths; a secret-looking value or a
    sensitive field name fails the read."""
    bindings = provider_payload.get("bindings") or []
    if not isinstance(bindings, list):
        raise LaunchplaneSafetyError("invalid_response")
    drops = _FieldDrops()
    projected_bindings: list[dict[str, object]] = []
    omitted_binding_count = 0
    for binding_value in bindings[:PRODUCT_SECRET_BINDINGS_MAX]:
        projected_binding = _project_product_secret_binding(binding_value, drops)
        if projected_binding is None:
            omitted_binding_count += 1
        else:
            projected_bindings.append(projected_binding)
    projected: dict[str, object] = {
        "product": public_identifier(provider_payload.get("product")),
        "bindings": projected_bindings,
        "bindings_truncated": len(bindings) > PRODUCT_SECRET_BINDINGS_MAX,
        "omitted_binding_count": omitted_binding_count,
        "dropped_field_count": drops.count,
        "dropped_field_paths": sorted(drops.paths),
    }
    assert_public_safe_shape(projected)
    return projected


TARGET_REPLACEMENT_OPERATION_TIMESTAMPS = (
    "created_at",
    "updated_at",
    "started_at",
    "heartbeat_at",
    "finished_at",
)
# Record fields the service writes that the read leaves out by design: request settings,
# idempotency material, lease holders, authorization and cancellation evidence, the poll URL,
# and free-text error messages, which can name hosts and settings that no filter catches.
TARGET_REPLACEMENT_OPERATION_OMITTED_FIELDS = frozenset(
    {
        "error_message",
        "schema_version",
        "free_text_omitted",
        "idempotency_key",
        "idempotency_scope",
        "request_fingerprint",
        "authorization",
        "lease_owner",
        "lease_expires_at",
        "cancellation",
        "runner_trace_id",
        "poll_url",
    }
)
TARGET_REPLACEMENT_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "product",
        "instance",
        "strategy",
        "allow_empty_data",
        "data_source_mode",
        "confirmation",
        "artifact_id",
        "source_git_ref",
        "expected_current_artifact_id",
        "verify_health",
        "verify_canonical",
        "verify_logo",
        "no_cache",
        "timeout_seconds",
        "health_timeout_seconds",
    }
)
TARGET_REPLACEMENT_RESULT_STATUSES = (
    "deploy_status",
    "post_deploy_status",
    "health_status",
    "canonical_status",
    "logo_status",
)
TARGET_REPLACEMENT_RESULT_IDS = ("deployment_record_id", "release_tuple_id", "artifact_id")
TARGET_REPLACEMENT_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "product",
        "context",
        "instance",
        "strategy",
        "post_deploy_override_status",
        "post_deploy_override_record_found",
        "post_deploy_override_payload_rendered",
        "post_deploy_override_count",
        "post_deploy_website_bootstrap_included",
        "post_deploy_override_evidence",
        "health_url",
        "canonical_url",
        "logo_urls",
        "verification_evidence",
        "runtime_identity_injected",
        "target_id",
        "target_name",
        "image_reference",
        "runtime_source",
        "error_message",
        "error_code",
        "error_description",
        "error_detail_keys",
        *TARGET_REPLACEMENT_RESULT_STATUSES,
        *TARGET_REPLACEMENT_RESULT_IDS,
    }
)


def _report_dropped_fields(
    source: dict[str, Any], *, prefix: str, kept: set[str], known: frozenset[str], drops: _FieldDrops
) -> None:
    """Count every non-empty field the projection leaves out. Known record fields are named;
    any other key is reported as ``<unlisted field>`` so a service key name is never echoed."""
    for key, value in source.items():
        if key in kept or value in (None, "", [], {}):
            continue
        drops.drop(f"{prefix}.{key}" if key in known else f"{prefix}.<unlisted field>")


def _project_target_replacement_result(
    value: object, drops: _FieldDrops
) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        drops.drop("result")
        return None
    result = cast(dict[str, Any], value)
    projected: dict[str, object] = {
        name: drops.keep(f"result.{name}", _public_dotted_code, result.get(name))
        for name in TARGET_REPLACEMENT_RESULT_STATUSES
    }
    for name in TARGET_REPLACEMENT_RESULT_IDS:
        projected[name] = drops.keep(f"result.{name}", public_identifier, result.get(name))
    image_reference = result.get("image_reference")
    projected["image_digest"] = drops.keep(
        "result.image_reference", public_identifier, _image_digest(image_reference)
    )
    _report_dropped_fields(
        result,
        prefix="result",
        kept={*TARGET_REPLACEMENT_RESULT_STATUSES, *TARGET_REPLACEMENT_RESULT_IDS, "image_reference"},
        known=TARGET_REPLACEMENT_RESULT_FIELDS,
        drops=drops,
    )
    if image_reference not in {None, ""}:
        # The repository part of the reference is dropped; only the digest is kept.
        drops.drop("result.image_reference")
    return projected


def _public_operation_error_description(value: object) -> str:
    if not isinstance(value, str):
        raise LaunchplaneSafetyError("invalid_response")
    # Fixed service prose describes a credential refusal. Redact that word
    # rather than loosening the public-summary policy; _FieldDrops checks the raw shape.
    summary = re.sub(r"\bcredential\b(?!\s*[:=])", "[redacted]", value, flags=re.IGNORECASE)
    return public_summary_string(summary)


def _project_target_replacement_operation(provider_payload: dict[str, Any]) -> dict[str, object]:
    """One Odoo target-replacement operation's progress and bounded failure details. Error messages,
    settings, URLs, provider target names and evidence payloads are dropped and listed by
    path; a secret-looking value in a kept field fails the read."""
    operation = _require_dict(provider_payload.get("operation"))
    drops = _FieldDrops()
    raw_result = provider_payload.get("result")
    if raw_result is None:
        raw_result = operation.get("result")

    def field(name: str, validate: Any) -> object:
        return drops.keep(f"operation.{name}", validate, operation.get(name))

    attempt = operation.get("attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        if attempt is not None:
            drops.drop("operation.attempt")
        attempt = None
    request_value = operation.get("request")
    request = request_value if isinstance(request_value, dict) else {}
    projected_operation: dict[str, object] = {
        "operation_id": public_identifier(operation.get("operation_id")),
        "product": public_identifier(operation.get("product")),
        "context": public_identifier(operation.get("context")),
        "instance": public_identifier(operation.get("instance")),
        "status": field("status", _public_dotted_code),
        "phase": field("phase", _public_dotted_code),
        "attempt": attempt,
        "deployment_record_id": field("deployment_record_id", public_identifier),
        "artifact_id": drops.keep(
            "operation.request.artifact_id", public_identifier, request.get("artifact_id")
        ),
        "error_code": field("error_code", _public_dotted_code),
        "error_description": field("error_description", _public_operation_error_description),
        "error_detail_keys": _project_env_key_names(
            operation.get("error_detail_keys"), path="operation.error_detail_keys", drops=drops
        ),
    }
    for name in TARGET_REPLACEMENT_OPERATION_TIMESTAMPS:
        projected_operation[name] = field(
            name, lambda value: public_summary_string(value, max_length=64)
        )
    # The operation's own copy of the result is read below, from the response's result.
    kept = {*projected_operation, "request", "result"}
    kept.discard("artifact_id")
    _report_dropped_fields(
        operation,
        prefix="operation",
        kept=kept,
        known=TARGET_REPLACEMENT_OPERATION_OMITTED_FIELDS,
        drops=drops,
    )
    _report_dropped_fields(
        request,
        prefix="operation.request",
        kept={"artifact_id"},
        known=TARGET_REPLACEMENT_REQUEST_FIELDS,
        drops=drops,
    )
    projected: dict[str, object] = {
        "operation": projected_operation,
        "result": _project_target_replacement_result(raw_result, drops),
        "dropped_field_count": drops.count,
        "dropped_field_paths": sorted(drops.paths),
    }
    assert_public_safe_shape(projected)
    return projected


TARGET_REPLACEMENT_PLAN_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
TARGET_REPLACEMENT_PLAN_MAX_LIST_ITEMS = 256
TARGET_REPLACEMENT_PLAN_CODES = ("plan_status", "strategy", "data_source_mode")
TARGET_REPLACEMENT_PLAN_IDS = (
    "product",
    "context",
    "instance",
    "expected_artifact_id",
    "expected_source_git_ref",
)
TARGET_REPLACEMENT_PLAN_FLAGS = (
    "target_record_found",
    "target_id_record_found",
    "inventory_found",
    "allow_empty_data",
)
TARGET_REPLACEMENT_PLAN_KEY_LISTS = ("retired_provider_keys", "delivered_runtime_keys")
# Plan fields the read leaves out by design: free-text blocker messages and warnings
# (both are counted), the next target's name, its domains and the approval issue URL.
TARGET_REPLACEMENT_PLAN_OMITTED_FIELDS = frozenset(
    {
        "blockers",
        "warnings",
        "expected_next_target_name",
        "expected_domain_hosts",
        "approval_issue_url",
    }
)
TARGET_REPLACEMENT_CURRENT_TARGET_KEY_LISTS = ("env_keys", "required_volume_keys_missing")
TARGET_REPLACEMENT_CURRENT_TARGET_FIELDS = frozenset(
    {
        "target_type",
        "target_id",
        "target_name",
        "project_name",
        "source_type",
        "compose_path",
        "compose_file_sha256",
        "domain_hosts",
        "latest_deployment_status",
        "latest_deployment_id",
        "required_volume_keys_present",
        "live_volume_values",
        "runtime_identity_present",
        "runtime_identity_deployment_record_id",
    }
)


def _public_env_key(value: object) -> str:
    if not isinstance(value, str) or not TARGET_REPLACEMENT_PLAN_ENV_KEY_RE.fullmatch(value):
        raise LaunchplaneSafetyError("invalid_response")
    return value


def _project_env_key_names(value: object, *, path: str, drops: _FieldDrops) -> list[str]:
    """Env-key names only. A name that does not look like an env key is dropped, and a
    secret-looking one fails the read."""
    if value is None or value == "" or value == []:
        return []
    if not isinstance(value, list):
        drops.drop(path)
        return []
    if len(value) > TARGET_REPLACEMENT_PLAN_MAX_LIST_ITEMS:
        drops.drop(f"{path}[]")
    names = [
        drops.keep(f"{path}[]", _public_env_key, item)
        for item in value[:TARGET_REPLACEMENT_PLAN_MAX_LIST_ITEMS]
    ]
    return [name for name in names if isinstance(name, str) and name]


def _project_target_replacement_current_target(
    value: object, drops: _FieldDrops
) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        drops.drop("plan.current_target")
        return None
    target = cast(dict[str, Any], value)
    projected: dict[str, object] = {
        name: _project_env_key_names(target.get(name), path=f"plan.current_target.{name}", drops=drops)
        for name in TARGET_REPLACEMENT_CURRENT_TARGET_KEY_LISTS
    }
    _report_dropped_fields(
        target,
        prefix="plan.current_target",
        kept=set(TARGET_REPLACEMENT_CURRENT_TARGET_KEY_LISTS),
        known=TARGET_REPLACEMENT_CURRENT_TARGET_FIELDS,
        drops=drops,
    )
    return projected


def _project_target_replacement_blocker_keys(
    value: object, drops: _FieldDrops
) -> dict[str, list[str]]:
    if value is None or value == {}:
        return {}
    if not isinstance(value, dict):
        drops.drop("plan.blocker_keys")
        return {}
    projected: dict[str, list[str]] = {}
    for code, names in value.items():
        if (
            not isinstance(code, str)
            or not PRODUCT_ACTIVITY_CODE_RE.fullmatch(code)
            or is_denied_key(code)
        ):
            drops.drop("plan.blocker_keys.<unlisted field>")
            continue
        projected[code] = _project_env_key_names(names, path=f"plan.blocker_keys.{code}", drops=drops)
    return projected


def _project_target_replacement_steps(value: object, drops: _FieldDrops) -> list[dict[str, object]]:
    if value is None or value == []:
        return []
    if not isinstance(value, list):
        drops.drop("plan.steps")
        return []
    if len(value) > TARGET_REPLACEMENT_PLAN_MAX_LIST_ITEMS:
        drops.drop("plan.steps[]")
    steps: list[dict[str, object]] = []
    for step in value[:TARGET_REPLACEMENT_PLAN_MAX_LIST_ITEMS]:
        if not isinstance(step, dict):
            drops.drop("plan.steps[]")
            continue
        step_id = drops.keep("plan.steps[].step_id", _public_dotted_code, step.get("step_id"))
        if not step_id:
            continue
        steps.append(
            {
                "step_id": step_id,
                "status": drops.keep("plan.steps[].status", _public_dotted_code, step.get("status")),
            }
        )
        _report_dropped_fields(
            step,
            prefix="plan.steps[]",
            kept={"step_id", "status"},
            known=frozenset({"message"}),
            drops=drops,
        )
    return steps


def _project_target_replacement_plan(plan_value: object) -> dict[str, object]:
    """An Odoo target-replacement plan's status, key names and blocker codes. Blocker,
    step and warning text, domains, target names and ids, volume values and URLs are
    dropped and listed by path; a secret-looking value in a kept field fails the read."""
    plan = _require_dict(plan_value)
    drops = _FieldDrops()
    projected: dict[str, object] = {}
    for name in TARGET_REPLACEMENT_PLAN_CODES:
        projected[name] = drops.keep(f"plan.{name}", _public_dotted_code, plan.get(name))
    for name in TARGET_REPLACEMENT_PLAN_IDS:
        projected[name] = drops.keep(f"plan.{name}", public_identifier, plan.get(name))
    for name in TARGET_REPLACEMENT_PLAN_FLAGS:
        flag = plan.get(name)
        if flag is not None and not isinstance(flag, bool):
            drops.drop(f"plan.{name}")
        projected[name] = flag if isinstance(flag, bool) else None
    for name in TARGET_REPLACEMENT_PLAN_KEY_LISTS:
        # None when the service does not report the list, as distinct from an empty one.
        projected[name] = (
            None
            if plan.get(name) is None
            else _project_env_key_names(plan.get(name), path=f"plan.{name}", drops=drops)
        )
    codes = plan.get("blocker_codes")
    if codes is not None and not isinstance(codes, list):
        drops.drop("plan.blocker_codes")
        codes = None
    if codes is not None and len(codes) > TARGET_REPLACEMENT_PLAN_MAX_LIST_ITEMS:
        drops.drop("plan.blocker_codes[]")
    kept_codes = [
        drops.keep("plan.blocker_codes[]", _public_dotted_code, item)
        for item in (codes or [])[:TARGET_REPLACEMENT_PLAN_MAX_LIST_ITEMS]
    ]
    projected["blocker_codes"] = [code for code in kept_codes if code]
    projected["blocker_keys"] = _project_target_replacement_blocker_keys(
        plan.get("blocker_keys"), drops
    )
    blockers = plan.get("blockers")
    projected["blocker_count"] = len(blockers) if isinstance(blockers, list) else 0
    warnings = plan.get("warnings")
    projected["warning_count"] = len(warnings) if isinstance(warnings, list) else 0
    projected["current_target"] = _project_target_replacement_current_target(
        plan.get("current_target"), drops
    )
    projected["steps"] = _project_target_replacement_steps(plan.get("steps"), drops)
    kept = {
        *TARGET_REPLACEMENT_PLAN_CODES,
        *TARGET_REPLACEMENT_PLAN_IDS,
        *TARGET_REPLACEMENT_PLAN_FLAGS,
        *TARGET_REPLACEMENT_PLAN_KEY_LISTS,
        "blocker_codes",
        "blocker_keys",
        "current_target",
        "steps",
    }
    _report_dropped_fields(
        plan,
        prefix="plan",
        kept=kept,
        known=TARGET_REPLACEMENT_PLAN_OMITTED_FIELDS,
        drops=drops,
    )
    projected["dropped_field_count"] = drops.count
    projected["dropped_field_paths"] = sorted(drops.paths)
    assert_public_safe_shape(projected)
    return projected


def preview_id_for(*, context: str, repository: str, pr_number: int) -> str:
    """Launchplane's generate_preview_id, with anchor_repo being the repository's name."""
    owner, separator, repo = repository.strip().partition("/")
    if not separator or not owner.strip() or not repo.strip() or "/" in repo.strip():
        raise ValueError("invalid_repository")
    if not context.strip():
        raise ValueError("invalid_context")
    if pr_number < 1:
        raise ValueError("invalid_pr")
    preview_key = f"{context.strip()}-{repo.strip()}-pr-{pr_number}".lower()
    normalized_key = re.sub(r"[^a-z0-9]+", "-", preview_key).strip("-")
    return f"preview-{normalized_key}"


def _preview_history_selector(args: argparse.Namespace) -> str:
    derived = (args.context, args.repository, args.pr)
    if args.preview_id:
        if any(value is not None for value in derived):
            raise ValueError("preview_selector_conflict")
        return args.preview_id
    if any(value is None for value in derived):
        raise ValueError("preview_selector_required")
    return preview_id_for(context=args.context, repository=args.repository, pr_number=args.pr)


def _image_digest(image_reference: object) -> str:
    if not isinstance(image_reference, str) or "@" not in image_reference:
        return ""
    return image_reference.rsplit("@", 1)[1]


def _project_preview_generation(
    generation_value: object, drops: _FieldDrops
) -> dict[str, object] | None:
    if not isinstance(generation_value, dict):
        return None
    generation = cast(dict[str, Any], generation_value)
    generation_id = drops.keep(
        "generations[].generation_id", public_identifier, generation.get("generation_id")
    )
    if not generation_id:
        return None
    sequence = generation.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        drops.drop("generations[].sequence")
        sequence = None

    def field(name: str, validate: Any) -> object:
        return drops.keep(f"generations[].{name}", validate, generation.get(name))

    def timestamp(name: str) -> object:
        return field(name, lambda value: public_summary_string(value, max_length=64))

    anchor = generation.get("anchor_summary")
    anchor = anchor if isinstance(anchor, dict) else {}
    sources = generation.get("source_map")
    sources = sources if isinstance(sources, list) else []
    projected_sources: list[dict[str, object]] = []
    for source_value in sources[:PREVIEW_MAX_SOURCES]:
        source = source_value if isinstance(source_value, dict) else {}
        projected_sources.append(
            {
                "repo": drops.keep(
                    "generations[].source_map[].repo", public_identifier, source.get("repo")
                ),
                "git_sha": drops.keep(
                    "generations[].source_map[].git_sha", public_identifier, source.get("git_sha")
                ),
                "selection": drops.keep(
                    "generations[].source_map[].selection",
                    _public_dotted_code,
                    source.get("selection"),
                ),
            }
        )
    identity = generation.get("runtime_identity")
    projected_identity: dict[str, object] | None = None
    if isinstance(identity, dict):
        image_reference = drops.keep(
            "generations[].runtime_identity.image_reference",
            public_identifier,
            identity.get("image_reference"),
        )
        projected_identity = {
            "deployment_record_id": drops.keep(
                "generations[].runtime_identity.deployment_record_id",
                public_identifier,
                identity.get("deployment_record_id"),
            ),
            "artifact_id": drops.keep(
                "generations[].runtime_identity.artifact_id",
                public_identifier,
                identity.get("artifact_id"),
            ),
            "source_git_ref": drops.keep(
                "generations[].runtime_identity.source_git_ref",
                public_identifier,
                identity.get("source_git_ref"),
            ),
            "image_reference": image_reference,
            "image_digest": _image_digest(image_reference),
            "deployed_at": drops.keep(
                "generations[].runtime_identity.deployed_at",
                lambda value: public_summary_string(value, max_length=64),
                identity.get("deployed_at"),
            ),
        }
    return {
        "generation_id": generation_id,
        "sequence": sequence,
        "state": field("state", _public_dotted_code),
        "requested_reason": field("requested_reason", _public_dotted_code),
        "requested_at": timestamp("requested_at"),
        "started_at": timestamp("started_at"),
        "ready_at": timestamp("ready_at"),
        "finished_at": timestamp("finished_at"),
        "failed_at": timestamp("failed_at"),
        "superseded_at": timestamp("superseded_at"),
        "artifact_id": field("artifact_id", public_identifier),
        "anchor_head_sha": drops.keep(
            "generations[].anchor_summary.head_sha", public_identifier, anchor.get("head_sha")
        ),
        "source_map": projected_sources,
        "deploy_status": field("deploy_status", _public_dotted_code),
        "verify_status": field("verify_status", _public_dotted_code),
        "overall_health_status": field("overall_health_status", _public_dotted_code),
        "failure_stage": field("failure_stage", _public_dotted_code),
        "failure_summary": field(
            "failure_summary", lambda value: public_summary_string(value, max_length=300)
        ),
        "runtime_identity": projected_identity,
    }


def _project_preview_history(provider_payload: dict[str, Any]) -> dict[str, object]:
    """The preview and its newest generations. Odd fields are dropped and unusable
    generations omitted, with counts and field paths; a secret-looking value fails the read."""
    preview = _require_dict(provider_payload.get("preview"))
    generations = provider_payload.get("generations") or []
    if not isinstance(generations, list):
        raise LaunchplaneSafetyError("invalid_response")
    drops = _FieldDrops()

    def field(name: str, validate: Any) -> object:
        return drops.keep(f"preview.{name}", validate, preview.get(name))

    def timestamp(name: str) -> object:
        return field(name, lambda value: public_summary_string(value, max_length=64))

    pr_number = preview.get("anchor_pr_number")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int):
        drops.drop("preview.anchor_pr_number")
        pr_number = None

    def newest_first(item: object) -> int:
        sequence = item.get("sequence") if isinstance(item, dict) else None
        return sequence if isinstance(sequence, int) and not isinstance(sequence, bool) else 0

    ordered = sorted(generations, key=newest_first, reverse=True)
    projected_generations: list[dict[str, object]] = []
    omitted_generation_count = 0
    for generation_value in ordered[:PREVIEW_MAX_GENERATIONS]:
        projected_generation = _project_preview_generation(generation_value, drops)
        if projected_generation is None:
            omitted_generation_count += 1
        else:
            projected_generations.append(projected_generation)
    projected: dict[str, object] = {
        "preview": {
            "preview_id": public_identifier(preview.get("preview_id")),
            "context": field("context", public_identifier),
            "anchor_repo": field("anchor_repo", public_identifier),
            "anchor_pr_number": pr_number,
            "anchor_pr_url": field("anchor_pr_url", public_url),
            "canonical_url": field("canonical_url", public_url),
            "state": field("state", _public_dotted_code),
            "created_at": timestamp("created_at"),
            "updated_at": timestamp("updated_at"),
            "destroyed_at": timestamp("destroyed_at"),
            "destroy_reason": field("destroy_reason", _public_dotted_code),
            "active_generation_id": field("active_generation_id", public_identifier),
            "serving_generation_id": field("serving_generation_id", public_identifier),
            "latest_generation_id": field("latest_generation_id", public_identifier),
        },
        "generations": projected_generations,
        "generations_truncated": len(generations) > PREVIEW_MAX_GENERATIONS,
        "omitted_generation_count": omitted_generation_count,
        "dropped_field_count": drops.count,
        "dropped_field_paths": sorted(drops.paths),
    }
    assert_public_safe_shape(projected)
    return projected


def _project_product_repository_identity(value: object) -> dict[str, object]:
    source = _require_dict(value)
    if any(str(key) not in PRODUCT_REPOSITORY_IDENTITY_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    projected: dict[str, object] = {}
    for field in ("repository_id", "repository_owner_id"):
        identifier = source.get(field, "")
        # GitHub ids are bounded decimals; an identity not yet recorded is empty.
        if not isinstance(identifier, str) or (
            identifier and not re.fullmatch(r"[1-9][0-9]{0,19}", identifier)
        ):
            raise LaunchplaneSafetyError("invalid_response")
        projected[field] = identifier
    return projected


def _project_product_repository_identity_plan(result: object) -> dict[str, object]:
    source = _require_dict(result)
    if any(str(key) not in PRODUCT_REPOSITORY_IDENTITY_PLAN_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    operation = source.get("operation")
    if operation not in PRODUCT_REPOSITORY_IDENTITY_OPERATIONS:
        raise LaunchplaneSafetyError("invalid_response")
    inventory_revision = source.get("inventory_revision")
    if (
        not isinstance(inventory_revision, int)
        or isinstance(inventory_revision, bool)
        or inventory_revision < 1
    ):
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "status": public_code(source.get("status")),
        "mode": _reviewed_plan_mode(source.get("mode")),
        "product": public_identifier(source.get("product")),
        "repository": public_identifier(source.get("repository")),
        "operation": operation,
        "identity_before": _project_product_repository_identity(source.get("identity_before")),
        "identity_after": _project_product_repository_identity(source.get("identity_after")),
        "inventory_record_id": public_identifier(source.get("inventory_record_id")),
        "inventory_revision": inventory_revision,
        "inventory_digest": _project_sha256(source.get("inventory_digest")),
        "reason": public_summary_string(source.get("reason")),
        "source_label": public_identifier(source.get("source_label")),
        "profile_record_sha256_before": _optional_sha256(
            source.get("profile_record_sha256_before")
        ),
        "plan_sha256": _optional_sha256(source.get("plan_sha256")),
        "changed": bool(_optional_bool(source.get("changed"))),
        "applied": bool(_optional_bool(source.get("applied"))),
    }
    for field in ("profile_updated_at_before", "profile_updated_at_after"):
        if source.get(field):
            projected[field] = public_summary_string(source.get(field), max_length=64)
    if source.get("read_back") is not None:
        projected["read_back"] = _project_product_repository_identity(source["read_back"])
    read_back_matches = _optional_bool(source.get("read_back_matches"))
    if read_back_matches is not None:
        projected["read_back_matches"] = read_back_matches
    assert_public_safe_shape(projected)
    return projected


def _project_odoo_addon_settings_result(result: object) -> dict[str, object]:
    source = _require_dict(result)
    if any(str(key) not in ODOO_ADDON_SETTINGS_RESULT_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    projected: dict[str, object] = {
        "status": public_code(source.get("status")),
        "mode": public_code(source.get("mode")),
        "product": public_identifier(source.get("product")),
        "context": public_identifier(source.get("context")),
        "instance": public_identifier(source.get("instance")),
        "addon": public_code(source.get("addon")),
        "rendered_action": public_code(source.get("rendered_action")),
        "reason": public_summary_string(source.get("reason")),
        "source_label": public_identifier(source.get("source_label")),
        "record_sha256_before": _optional_sha256(source.get("record_sha256_before")),
        "record_sha256_after": _optional_sha256(source.get("record_sha256_after")),
        "plan_sha256": _optional_sha256(source.get("plan_sha256")),
    }
    for key in ("production_lane", "record_exists", "changed", "applied"):
        projected[key] = bool(_optional_bool(source.get(key)))
    read_back_matches = _optional_bool(source.get("read_back_matches"))
    if read_back_matches is not None:
        projected["read_back_matches"] = read_back_matches
    changes = source.get("changes", [])
    if not isinstance(changes, list):
        raise LaunchplaneSafetyError("invalid_response")
    projected_changes: list[dict[str, object]] = []
    for change_value in changes:
        change = _require_dict(change_value)
        if any(str(key) not in {"setting", "action", "before", "after"} for key in change):
            raise LaunchplaneSafetyError("unsafe_response_shape")
        projected_change: dict[str, object] = {
            "setting": public_code(change.get("setting")),
            "action": public_code(change.get("action")),
        }
        for side in ("before", "after"):
            if change.get(side) is not None:
                projected_change[side] = _project_odoo_addon_setting_evidence(change[side])
        projected_changes.append(projected_change)
    projected["changes"] = projected_changes
    read_back = source.get("read_back", [])
    if not isinstance(read_back, list):
        raise LaunchplaneSafetyError("invalid_response")
    projected["read_back"] = [_project_odoo_addon_setting_evidence(item) for item in read_back]
    next_actions = source.get("next_actions", [])
    if not isinstance(next_actions, list):
        raise LaunchplaneSafetyError("invalid_response")
    projected["next_actions"] = [public_summary_string(item) for item in next_actions]
    assert_public_safe_shape(projected)
    return projected


def _project_success_output(
    operation: str, provider_payload: dict[str, Any], *, request: dict[str, object] | None = None
) -> tuple[dict[str, object], dict[str, object]]:
    if operation in {
        "generic-web-deploy-recovery-dry-run",
        "generic-web-deploy-recovery-apply",
    }:
        return {}, _project_generic_web_deploy_recovery_result(
            provider_payload,
            operation=operation,
        )
    if any(str(key) not in SUCCESS_TOP_LEVEL_KEYS for key in provider_payload):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    replayed = provider_payload.get("replayed")
    if replayed is not None and not isinstance(replayed, bool):
        raise LaunchplaneSafetyError("invalid_response")
    if provider_payload.get("original_trace_id"):
        public_trace_id(provider_payload["original_trace_id"])
    if operation in {"product-expected-config-dry-run", "product-expected-config-apply"}:
        records = _project_records(provider_payload.get("records"), {"product_profile"})
        source = provider_payload.get("result")
        if not isinstance(source, dict) or set(source) != {
            "status", "mode", "product", "source_label", "changed",
            "runtime_environment_keys", "managed_secret_bindings", "summary",
        }:
            raise LaunchplaneSafetyError("invalid_response")
        expected_mode = "apply" if operation.endswith("-apply") else "dry-run"
        if source["status"] != "ok" or source["mode"] != expected_mode or not isinstance(source["changed"], bool):
            raise LaunchplaneSafetyError("invalid_response")
        expected_config_result: dict[str, object] = {
            "status": "ok", "mode": expected_mode,
            "product": public_identifier(source["product"]), "changed": source["changed"],
        }
        if not isinstance(source["summary"], dict):
            raise LaunchplaneSafetyError("invalid_response")
        # The submitted request, not the response, decides the shape: a removal
        # request needs every removal disposition (an older service that drops
        # them must not look like a reviewed removal), and an add-only request
        # keeps exactly added/unchanged.
        removal_shape = request is not None and request.get("removal_requested") is True
        if removal_shape:
            summary = source["summary"]
            for count_key in (
                "runtime_environment_key_remove_count", "managed_secret_binding_remove_count",
                "runtime_environment_key_absent_count", "managed_secret_binding_absent_count",
                "managed_secret_binding_still_bound_count",
            ):
                count = summary.get(count_key)
                if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    raise LaunchplaneSafetyError("invalid_response")
        for kind, removal_dispositions in (
            ("runtime_environment_keys", ("removed", "absent")),
            ("managed_secret_bindings", ("removed", "absent", "still_bound")),
        ):
            dispositions = ("added", "unchanged") + (removal_dispositions if removal_shape else ())
            changes = source[kind]
            if not isinstance(changes, dict) or set(changes) != set(dispositions):
                raise LaunchplaneSafetyError("invalid_response")
            for disposition in dispositions:
                items = changes[disposition]
                if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
                    raise LaunchplaneSafetyError("invalid_response")
                expected_config_result[f"{kind}_{disposition}_count"] = len(items)
        return records, expected_config_result
    if operation == "merge-train-controller-run-once":
        records = _project_records(
            provider_payload.get("records"),
            {
                "merge_train_batch_candidate_record_id",
                "merge_train_batch_landing_plan_record_id",
                "merge_train_stack_collapse_plan_record_id",
                "superseded_merge_train_batch_candidate_record_id",
                "merge_train_landing_plan_record_id",
                "merge_train_stack_collapse_record_id",
                "merge_train_feedback_record_id",
            },
        )
        return records, _project_merge_train_result(provider_payload.get("result"))
    if operation == "product-config-preflight":
        records = _project_records(
            provider_payload.get("records"),
            {
                "agent_write_intent_record_id",
                "intent_record_id",
                "product_config_record_id",
                "dry_run_record_id",
                "apply_record_id",
            },
        )
        return records, _project_product_config_preflight_result(
            provider_payload.get("result")
        )
    if operation in {"product-config-dry-run", "product-config-apply"}:
        records = _project_records(
            provider_payload.get("records"),
            {"product_config_record_id", "dry_run_record_id", "apply_record_id"},
        )
        result = provider_payload.get("result")
        if isinstance(result, dict) and "intent" in result:
            return records, _project_product_config_preflight_result(result)
        return records, _project_product_config_apply_result(result)
    if operation == "preview-feedback-remediation":
        records = _project_records(
            provider_payload.get("records"),
            {
                "preview_pr_feedback_remediation_id",
                "preview_pr_feedback_id",
            },
        )
        return records, _project_preview_feedback_remediation_result(
            provider_payload.get("result")
        )
    if operation in {"change-impact-policy-dry-run", "change-impact-policy-apply"}:
        records = _project_records(provider_payload.get("records"), set())
        return records, _project_change_impact_policy_result(
            provider_payload.get("result")
        )
    if operation in {"repository-inventory-dry-run", "repository-inventory-apply"}:
        records = _project_records(provider_payload.get("records"), set())
        return records, _project_repository_inventory_apply_result(
            provider_payload.get("result")
        )
    if operation in {"odoo-addon-settings-dry-run", "odoo-addon-settings-apply"}:
        records = _project_records(
            provider_payload.get("records"), {"product_profile", "context", "instance"}
        )
        return records, _project_odoo_addon_settings_result(provider_payload.get("result"))
    if operation in {"integration-allowances-dry-run", "integration-allowances-apply"}:
        records = _project_records(
            provider_payload.get("records"), {"product_profile", "context", "instance"}
        )
        return records, _project_integration_allowances_plan(provider_payload.get("result"))
    if operation in {"testing-hold-dry-run", "testing-hold-apply"}:
        records = _project_records(
            provider_payload.get("records"), {"product_profile", "context", "instance"}
        )
        return records, _project_testing_hold_plan(provider_payload.get("result"))
    if operation in {"product-repository-identity-dry-run", "product-repository-identity-apply"}:
        records = _project_records(
            provider_payload.get("records"), {"product_profile", "repository_inventory"}
        )
        return records, _project_product_repository_identity_plan(provider_payload.get("result"))
    if operation in {"product-owner-dry-run", "product-owner-apply"}:
        records = _project_records(provider_payload.get("records"), {"product_profile"})
        return records, _project_product_owner_plan(provider_payload.get("result"))
    if operation in {"product-image-repository-dry-run", "product-image-repository-apply"}:
        records = _project_records(provider_payload.get("records"), {"product_profile"})
        return records, _project_product_image_repository_plan(provider_payload.get("result"))
    if operation in {"production-backup-authority-dry-run", "production-backup-authority-apply"}:
        records = _project_records(provider_payload.get("records"), set())
        return records, _project_production_backup_authority_result(
            provider_payload.get("result")
        )
    if operation in {
        "dokploy-target-create-compose-dry-run",
        "dokploy-target-create-compose-apply",
        "dokploy-target-complete-compose-source-dry-run",
        "dokploy-target-complete-compose-source-apply",
    }:
        records = _project_records(provider_payload.get("records"), set())
        return records, _project_dokploy_compose_setup(
            provider_payload.get("result"), request=request
        )
    if operation in {"private-health-endpoint-dry-run", "private-health-endpoint-apply"}:
        records = _project_records(provider_payload.get("records"), set())
        return records, _project_private_health_endpoint_plan(
            provider_payload.get("result"), request=request
        )
    if operation == "product-promotion-dry-run":
        # Record ids, release URLs and target names are not projected; the result keeps statuses.
        return {}, _project_product_promotion_dry_run(provider_payload.get("result"))
    raise LaunchplaneSafetyError("invalid_response")


def summarize_change_impact_policy_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    if any(str(key) not in {"status", "trace_id", "read_model"} for key in provider_payload):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    status = public_code(provider_payload.get("status"), default="ok")
    payload = base_payload(
        status=status, operation="change-impact-policy-read", request=request
    )
    payload["result"] = _project_change_impact_policy_read_model(
        provider_payload.get("read_model")
    )
    payload["summary"] = {
        "launchplane_status": status,
        "trace_id": public_trace_id(provider_payload.get("trace_id")),
        "recommendation": "Use the active record metadata to verify the intended policy revision and digest.",
    }
    assert_public_safe_shape(payload["result"])
    assert_public_safe_shape(payload["summary"])
    return payload


def summarize_repository_inventory_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    if any(str(key) not in {"status", "trace_id", "read_model"} for key in provider_payload):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    status = public_code(provider_payload.get("status"), default="ok")
    payload = base_payload(
        status=status, operation="repository-inventory-read", request=request
    )
    payload["result"] = _project_repository_inventory_read_model(
        provider_payload.get("read_model")
    )
    payload["summary"] = {
        "launchplane_status": status,
        "trace_id": public_trace_id(provider_payload.get("trace_id")),
        "recommendation": (
            "Use the bounded current record metadata to prepare the next exact inventory revision."
        ),
    }
    assert_public_safe_shape(payload["result"])
    assert_public_safe_shape(payload["summary"])
    return payload


def _require_exact_fields(value: object, fields: set[str]) -> dict[str, Any]:
    source = _require_dict(value)
    if set(map(str, source)) != fields:
        raise LaunchplaneSafetyError("unsafe_response_shape")
    return source


def _error_payload_from_http(exc: urllib.error.HTTPError) -> dict[str, object]:
    try:
        raw = exc.read().decode()
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _status_for_http_error(code: int, provider_payload: dict[str, object]) -> str:
    error = provider_payload.get("error")
    error_code = ""
    if isinstance(error, dict):
        error_code = str(error.get("code") or "")
    if code == 401:
        return "unauthorized"
    if code == 403 or error_code == "authorization_denied":
        return "denied"
    if code in {400, 422}:
        return "invalid_request"
    if code == 409 or error_code in {
        "stale",
        "mismatched_intent",
        "matching_dry_run_required",
        "change_impact_policy_conflict",
    }:
        return "stale"
    return "unavailable"


def http_error_recommendation(status: str) -> str:
    recommendations = {
        "unauthorized": "Credential was not accepted; check the operator token source before retrying.",
        "denied": (
            "Credential was accepted but this action was denied; this is an authority-scope "
            "result. Report the trace ID, block only the affected work, and continue independent "
            "safe work when available. Escalate a missing capability to the owning authorization-"
            "architecture issue. Do not probe routes manually or route this call through a GitHub "
            "workflow, Actions secret, or OIDC role."
        ),
        "stale": "Refresh the dry-run or intent evidence before retrying this write action.",
        "invalid_request": "Fix the private request payload before retrying this write action.",
        "unavailable": "Launchplane service was unavailable or returned an invalid error envelope; retry later with trace evidence.",
    }
    return recommendations.get(status, "Stop and surface the compact Launchplane error.")


def summarize_success(
    *, operation: str, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    records, result = _project_success_output(operation, provider_payload, request=request)
    status = public_code(provider_payload.get("status"), default="accepted")
    payload = base_payload(status=status, operation=operation, request=request)
    payload["records"] = records
    payload["result"] = result

    summary: dict[str, object] = {
        "launchplane_status": status,
        "trace_id": public_trace_id(provider_payload.get("trace_id")),
        "recommendation": "Review the redacted result before deciding the next action.",
    }
    if operation == "merge-train-controller-run-once":
        controller_action = result.get("controller_action")
        if isinstance(controller_action, str):
            summary["controller_action"] = controller_action
            summary["recommendation"] = (
                "Stop and report this merge-train state."
                if controller_action in ATTENTION_CONTROLLER_ACTIONS
                else "Call the controller again only after reading this action."
            )
    else:
        intent = result.get("intent")
        if isinstance(intent, dict):
            summary["intent_status"] = intent.get("status")
            summary["reason_code"] = intent.get("reason_code")
            summary["safe_to_execute"] = intent.get("safe_to_execute")
            if intent.get("next_action"):
                summary["recommendation"] = intent["next_action"]
        elif operation in {"change-impact-policy-dry-run", "change-impact-policy-apply"}:
            summary["policy_apply_status"] = result.get("status")
            summary["recommendation"] = (
                "Review the redacted dry-run result before applying the exact same private payload."
                if operation == "change-impact-policy-dry-run"
                else "Read back the active change-impact policy before relying on it."
            )
        elif operation in {"repository-inventory-dry-run", "repository-inventory-apply"}:
            summary["inventory_apply_status"] = result.get("status")
            summary["inventory_digest"] = result.get("inventory_digest")
            summary["recommendation"] = (
                "Save and review this redacted dry-run evidence before applying the exact same private payload."
                if operation == "repository-inventory-dry-run"
                else "Read back the current repository inventory record before relying on the mutation."
            )
        elif operation in {"integration-allowances-dry-run", "integration-allowances-apply"}:
            summary["plan_sha256"] = result.get("plan_sha256")
            summary["recommendation"] = (
                "Save this redacted dry-run output, review the per-integration diff, then apply "
                "the exact same private payload with --expected-plan-digest."
                if operation == "integration-allowances-dry-run"
                else "Check read_back_matches. The allowances take effect at the lane's next "
                "integration read-back."
            )
        elif operation in {"testing-hold-dry-run", "testing-hold-apply"}:
            summary["plan_sha256"] = result.get("plan_sha256")
            summary["recommendation"] = (
                "Save this redacted dry-run output, review the hold change, then apply the same "
                "product, lane, hold direction and reason with --expected-plan-digest."
                if operation == "testing-hold-dry-run"
                else "Check read_back_matches. Lifting the hold requests a reconcile of the "
                "testing target (reconcile_requested)."
            )
        elif operation in {
            "product-repository-identity-dry-run",
            "product-repository-identity-apply",
        }:
            summary["plan_sha256"] = result.get("plan_sha256")
            summary["recommendation"] = (
                "Save this redacted dry-run output, review the identity copied from inventory, "
                "then apply the same product and reason with --expected-plan-digest."
                if operation == "product-repository-identity-dry-run"
                else "Check read_back_matches before relying on repository-id event routing."
            )
        elif operation in {"product-owner-dry-run", "product-owner-apply"}:
            summary["plan_sha256"] = result.get("plan_sha256")
            summary["recommendation"] = (
                "Save this redacted dry-run output, review the Client before and after, then apply "
                "the same product, login or --clear, and reason with --expected-plan-digest."
                if operation == "product-owner-dry-run"
                else "Check read_back_matches before relying on the recorded Client."
            )
        elif operation in {
            "product-image-repository-dry-run",
            "product-image-repository-apply",
        }:
            summary["plan_sha256"] = result.get("plan_sha256")
            summary["changed"] = result.get("changed")
            summary["recommendation"] = (
                "Save this redacted dry-run output, review the image repository before and after "
                "and each lane's artifact, then apply the same product, image repository and "
                "reason with --expected-plan-digest."
                if operation == "product-image-repository-dry-run"
                else "Check read_back_matches before relying on the new image repository."
            )
        elif operation in {
            "production-backup-authority-dry-run",
            "production-backup-authority-apply",
        }:
            summary["authority_digest"] = result.get("authority_digest")
            summary["recommendation"] = (
                "Save this redacted dry-run output, review the policy and targets, then apply the "
                "exact same private payload with --expected-plan-digest set to authority_digest."
                if operation == "production-backup-authority-dry-run"
                else "Check read_back_matches and the read-back state before relying on the policy."
            )
        elif operation in {
            "dokploy-target-create-compose-dry-run",
            "dokploy-target-create-compose-apply",
            "dokploy-target-complete-compose-source-dry-run",
            "dokploy-target-complete-compose-source-apply",
        }:
            summary["plan_sha256"] = result.get("plan_sha256")
            summary["recommendation"] = (
                "Save this redacted dry-run output, review the plan actions, then apply the exact "
                "same private payload with --expected-plan-digest."
                if operation.endswith("-dry-run")
                else "Check read_back_matches before relying on the target and its source. "
                "For a newly created target, add the lane through stable-lane repair."
            )
        elif operation in {
            "private-health-endpoint-dry-run",
            "private-health-endpoint-apply",
        }:
            summary["plan_sha256"] = result.get("plan_sha256")
            summary["recommendation"] = (
                "Save this redacted dry-run output, review the endpoint key and scope, then apply "
                "the exact same private payload and reason with --expected-plan-digest."
                if operation == "private-health-endpoint-dry-run"
                else "Check read_back_matches, then name the endpoint_key in the lane's "
                "private_http health check."
            )
        elif operation == "product-promotion-dry-run":
            summary["promotion_status"] = result.get("promotion_status")
            summary["backup_status"] = result.get("backup_status")
            summary["recommendation"] = (
                "Launchplane recorded this dry-run for the evidence fingerprint and bump. It made "
                "no backup and deployed nothing; a live promotion is not a helper command."
            )
        elif operation in {"odoo-addon-settings-dry-run", "odoo-addon-settings-apply"}:
            summary["plan_sha256"] = result.get("plan_sha256")
            summary["recommendation"] = (
                "Save this redacted dry-run output, review the diff, then apply the exact same "
                "private payload with --expected-plan-digest."
                if operation == "odoo-addon-settings-dry-run"
                else "Check read_back_matches, then run Odoo post-deploy for the lane and verify "
                "the settings in its database."
            )
        elif operation in {
            "generic-web-deploy-recovery-dry-run",
            "generic-web-deploy-recovery-apply",
        }:
            summary["recovery_digest"] = result.get("recovery_digest")
            summary["recovery_action"] = result.get(
                "proposed_action"
                if operation == "generic-web-deploy-recovery-dry-run"
                else "recovery_action"
            )
            summary["recommendation"] = (
                "Review the redacted dry-run result before applying the exact same private payload."
                if operation == "generic-web-deploy-recovery-dry-run"
                else "Verify the recovered reservation and a subsequent normal deploy before closing the recovery plan."
            )
    assert_public_safe_shape(summary)
    payload["summary"] = {key: value for key, value in summary.items() if value not in {"", None}}
    return payload


def summarize_http_error(
    *, operation: str, request: dict[str, object], exc: urllib.error.HTTPError
) -> dict[str, object]:
    provider_payload = _error_payload_from_http(exc)
    status = _status_for_http_error(exc.code, provider_payload)
    error = provider_payload.get("error")
    if not isinstance(error, dict):
        error = {}
    payload = base_payload(status=status, operation=operation, request=request)
    payload["summary"] = {
        "http_status": exc.code,
        "trace_id": public_trace_id(provider_payload.get("trace_id")),
        "error_code": public_code(error.get("code"), default=status),
        "recommendation": http_error_recommendation(status),
    }
    message = (
        "Launchplane read was rejected; inspect the trace in an approved operator surface."
        if operation in READ_ONLY_OPERATIONS
        else "Launchplane write action was rejected; inspect the trace in an approved operator surface."
    )
    payload["warnings"] = [warning(str(error.get("code") or status), message)]
    return payload


def emit_http_error_payload(
    *, operation: str, request: dict[str, object], exc: urllib.error.HTTPError
) -> None:
    try:
        emit(summarize_http_error(operation=operation, request=request, exc=exc))
    except LaunchplaneSafetyError:
        emit_invalid_response(operation=operation, request=request)


def emit_safety_error_payload(
    *, operation: str, request: dict[str, object], exc: LaunchplaneSafetyError
) -> None:
    unsafe_redirect = exc.code == "unsafe_redirect"
    emit(
        unavailable_payload(
            operation=operation,
            request=request,
            status="unavailable" if unsafe_redirect else "invalid",
            code=exc.code if unsafe_redirect else "invalid_response",
            message=(
                "Launchplane redirected to an unsafe destination."
                if unsafe_redirect
                else "Launchplane returned an invalid response."
            ),
        )
    )


def emit_provider_unavailable(
    *, operation: str, request: dict[str, object]
) -> None:
    emit(
        unavailable_payload(
            operation=operation,
            request=request,
            status="unavailable",
            code="provider_unavailable",
            message="Launchplane service is unavailable.",
        )
    )


def emit_invalid_response(*, operation: str, request: dict[str, object]) -> None:
    emit(
        unavailable_payload(
            operation=operation,
            request=request,
            status="invalid",
            code="invalid_response",
            message="Launchplane returned an invalid response.",
        )
    )


def read_payload_file(path: str) -> dict[str, object]:
    if path == "-":
        raise ValueError("stdin_payload_unsupported")
    payload_path = Path(path).expanduser()
    try:
        absolute_payload_path = absolute_path_without_symlink_resolution(payload_path)
        resolved_payload_path = payload_path.resolve(strict=True)
        repo_root = active_repo_root()
    except OSError:
        raise ValueError("invalid_payload_path") from None
    if _is_relative_to(absolute_payload_path, repo_root) or _is_relative_to(
        resolved_payload_path, repo_root
    ):
        raise ValueError("repo_local_payload_unsupported")
    try:
        raw = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid_payload") from None
    if not isinstance(raw, dict):
        raise ValueError("invalid_payload")
    return raw


def metadata_review_digest(body: dict[str, object]) -> str:
    review_subject = dict(body)
    review_subject.pop("mode", None)
    return hashlib.sha256(
        json.dumps(review_subject, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def repository_inventory_idempotency_key_fingerprint(idempotency_key: str) -> str:
    normalized = idempotency_key.strip()
    if not normalized:
        raise ValueError("idempotency_key_required")
    return f"sha256:{hashlib.sha256(normalized.encode()).hexdigest()}"


def _require_apply_eligible_repository_inventory_dry_run(
    args: argparse.Namespace,
    *,
    expected_inventory_digest: str,
    expected_payload_digest: str,
    expected_inventory_revision: int,
    expected_idempotency_key_fingerprint: str,
) -> None:
    evidence_path = str(getattr(args, "dry_run_evidence_file", "") or "").strip()
    if not evidence_path:
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    try:
        evidence = read_payload_file(evidence_path)
    except ValueError:
        raise ValueError("reviewed_dry_run_not_apply_eligible") from None
    request = evidence.get("request")
    result = evidence.get("result")
    if not isinstance(request, dict) or not isinstance(result, dict):
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    if (
        evidence.get("operation") != "repository-inventory-dry-run"
        or evidence.get("status") != "ok"
        or request.get("mode") != "dry_run"
        or request.get("payload_source") != "private_file"
        or request.get("payload_digest") != expected_payload_digest
        or request.get("idempotency_key_fingerprint")
        != expected_idempotency_key_fingerprint
        or result.get("status") != "would_apply"
        or result.get("mode") != "dry_run"
        or result.get("inventory_digest") != expected_inventory_digest
        or result.get("inventory_revision") != expected_inventory_revision
    ):
        raise ValueError("reviewed_dry_run_not_apply_eligible")


def repository_inventory_payload_body(
    args: argparse.Namespace, *, mode: str
) -> dict[str, object]:
    body = read_payload_file(args.payload_file)
    if body.get("schema_version") != 1:
        raise ValueError("schema_version_required")
    record = body.get("record")
    if not isinstance(record, dict):
        raise ValueError("record_required")
    repository_id = record.get("repository_id")
    if (
        not isinstance(repository_id, str)
        or not repository_id.isdecimal()
        or int(repository_id) < 1
    ):
        raise ValueError("repository_id_required")
    inventory_revision = record.get("inventory_revision")
    if (
        not isinstance(inventory_revision, int)
        or isinstance(inventory_revision, bool)
        or inventory_revision < 1
    ):
        raise ValueError("inventory_revision_required")
    reason = record.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason_required")
    expected_current_record_id = body.get("expected_current_record_id")
    if not isinstance(expected_current_record_id, str):
        raise ValueError("invalid_expected_current_record_id")
    payload_digest = metadata_review_digest(body)
    if mode == "apply":
        _require_idempotency(args)
        if not args.reviewed_dry_run:
            raise ValueError("reviewed_dry_run_required")
        expected_inventory_digest = args.expected_inventory_digest.strip().lower()
        if len(expected_inventory_digest) != 64 or any(
            character not in "0123456789abcdef" for character in expected_inventory_digest
        ):
            raise ValueError("invalid_expected_inventory_digest")
        raw_inventory_digest = record.get("inventory_digest")
        if raw_inventory_digest is not None and not isinstance(raw_inventory_digest, str):
            raise ValueError("invalid_inventory_digest")
        embedded_inventory_digest = (
            raw_inventory_digest.strip().lower()
            if isinstance(raw_inventory_digest, str)
            else ""
        )
        if embedded_inventory_digest and embedded_inventory_digest != expected_inventory_digest:
            raise ValueError("inventory_digest_mismatch")
        _require_apply_eligible_repository_inventory_dry_run(
            args,
            expected_inventory_digest=expected_inventory_digest,
            expected_payload_digest=payload_digest,
            expected_inventory_revision=inventory_revision,
            expected_idempotency_key_fingerprint=(
                repository_inventory_idempotency_key_fingerprint(args.idempotency_key)
            ),
        )
    body["mode"] = mode
    return body


def _require_idempotency(args: argparse.Namespace) -> None:
    if not args.idempotency_key.strip():
        raise ValueError("idempotency_key_required")


def _require_apply_eligible_recovery_dry_run(
    args: argparse.Namespace,
    *,
    expected_recovery_digest: str,
    expected_product: str,
    expected_instance: str,
) -> None:
    evidence_path = str(getattr(args, "dry_run_evidence_file", "") or "").strip()
    if not evidence_path:
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    try:
        evidence = read_payload_file(evidence_path)
    except ValueError:
        raise ValueError("reviewed_dry_run_not_apply_eligible") from None
    result = evidence.get("result")
    if not isinstance(result, dict):
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    if (
        evidence.get("operation") != "generic-web-deploy-recovery-dry-run"
        or evidence.get("status") != "ok"
        or result.get("status") != "ok"
        or result.get("mode") != "dry-run"
        or result.get("proposed_action")
        not in {"adopt_observed", "retry_original_operation"}
        or result.get("provider_outcome") not in {"present", "absent"}
        or result.get("retry_safe") is not True
        or result.get("recovery_digest") != expected_recovery_digest
        or result.get("product") != expected_product
        or result.get("instance") != expected_instance
    ):
        raise ValueError("reviewed_dry_run_not_apply_eligible")


def _require_apply_eligible_odoo_addon_settings_dry_run(
    args: argparse.Namespace,
    *,
    expected_plan_digest: str,
    expected_product: str,
    expected_context: str,
    expected_instance: str,
) -> None:
    evidence_path = str(getattr(args, "dry_run_evidence_file", "") or "").strip()
    if not evidence_path:
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    try:
        evidence = read_payload_file(evidence_path)
    except ValueError:
        raise ValueError("reviewed_dry_run_not_apply_eligible") from None
    result = evidence.get("result")
    if not isinstance(result, dict):
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    if (
        evidence.get("operation") != "odoo-addon-settings-dry-run"
        or evidence.get("status") != "accepted"
        or result.get("status") != "ok"
        or result.get("mode") != "dry-run"
        or result.get("plan_sha256") != expected_plan_digest
        or result.get("product") != expected_product
        or result.get("context") != expected_context
        or result.get("instance") != expected_instance
    ):
        raise ValueError("reviewed_dry_run_not_apply_eligible")


def _require_apply_eligible_integration_allowances_dry_run(
    args: argparse.Namespace,
    *,
    expected_plan_digest: str,
    expected_product: str,
    expected_context: str,
    expected_instance: str,
) -> None:
    evidence_path = str(getattr(args, "dry_run_evidence_file", "") or "").strip()
    if not evidence_path:
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    try:
        evidence = read_payload_file(evidence_path)
    except ValueError:
        raise ValueError("reviewed_dry_run_not_apply_eligible") from None
    result = evidence.get("result")
    if not isinstance(result, dict):
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    if (
        evidence.get("operation") != "integration-allowances-dry-run"
        or evidence.get("status") != "accepted"
        or result.get("status") != "ok"
        or result.get("mode") != "dry-run"
        or result.get("plan_sha256") != expected_plan_digest
        or result.get("product") != expected_product
        or result.get("context") != expected_context
        or result.get("instance") != expected_instance
    ):
        raise ValueError("reviewed_dry_run_not_apply_eligible")


def integration_allowances_body(args: argparse.Namespace, *, mode: str) -> dict[str, object]:
    body = read_payload_file(args.payload_file)
    if any(str(key) not in INTEGRATION_ALLOWANCES_PAYLOAD_FIELDS for key in body):
        raise ValueError("unsupported_payload_field")
    if body.get("schema_version") != 1:
        raise ValueError("schema_version_required")
    identity: dict[str, str] = {}
    for field in ("product", "context", "instance", "reason"):
        value = body.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field}_required")
        identity[field] = value.strip()
    allowances = body.get("allowances")
    if not isinstance(allowances, list):
        raise ValueError("allowances_list_required")
    for allowance in allowances:
        if not isinstance(allowance, dict) or any(
            str(key) not in INTEGRATION_ALLOWANCE_INPUT_FIELDS for key in allowance
        ):
            raise ValueError("unsupported_allowance_field")
        if allowance.get("kind") not in INTEGRATION_ALLOWANCE_KINDS:
            raise ValueError("unsupported_allowance_kind")
    body["mode"] = mode
    if mode == "apply":
        _require_idempotency(args)
        if not args.reviewed_dry_run:
            raise ValueError("reviewed_dry_run_required")
        expected_plan_digest = args.expected_plan_digest.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_plan_digest):
            raise ValueError("invalid_expected_plan_digest")
        _require_apply_eligible_integration_allowances_dry_run(
            args,
            expected_plan_digest=expected_plan_digest,
            expected_product=identity["product"],
            expected_context=identity["context"].lower(),
            expected_instance=identity["instance"].lower(),
        )
        body["reviewed_plan_sha256"] = expected_plan_digest
    return body


def _summarize_lane_config_read(
    *,
    operation: str,
    request: dict[str, object],
    provider_payload: dict[str, Any],
    project: Any,
    recommendation: str,
) -> dict[str, object]:
    if any(
        str(key) not in {"status", "trace_id", "records", "result"} for key in provider_payload
    ):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    status = public_code(provider_payload.get("status"), default="ok")
    payload = base_payload(status=status, operation=operation, request=request)
    payload["records"] = _project_records(
        provider_payload.get("records"), {"product_profile", "context", "instance"}
    )
    payload["result"] = project(provider_payload.get("result"))
    payload["summary"] = {
        "launchplane_status": status,
        "trace_id": public_trace_id(provider_payload.get("trace_id")),
        "recommendation": recommendation,
    }
    assert_public_safe_shape(payload["summary"])
    return payload


def summarize_integration_allowances_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    return _summarize_lane_config_read(
        operation="integration-allowances-read",
        request=request,
        provider_payload=provider_payload,
        project=_project_integration_allowances_read,
        recommendation=(
            "Change allowances with integration-allowances-dry-run and "
            "integration-allowances-apply; the request carries the lane's whole list."
        ),
    )


def summarize_testing_hold_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    return _summarize_lane_config_read(
        operation="testing-hold-read",
        request=request,
        provider_payload=provider_payload,
        project=_project_testing_hold_read,
        recommendation=(
            "Set or lift the hold with testing-hold-dry-run and testing-hold-apply."
        ),
    )


LANE_CONFIG_READ_SUMMARIZERS = {
    "integration-allowances-read": summarize_integration_allowances_read,
    "testing-hold-read": summarize_testing_hold_read,
}


def execute_lane_config_read(
    *, args: argparse.Namespace, operation: str, request: dict[str, object]
) -> int:
    summarize = LANE_CONFIG_READ_SUMMARIZERS[operation]
    settings = prepare_operator_settings(args=args, operation=operation, request=request)
    if settings is None:
        return 2
    try:
        provider_payload = request_launchplane_read(
            service_url=settings["service_url"],
            path=helper_command_path(operation),
            settings=settings,
            query={
                "product": args.product,
                "context": args.context,
                "instance": args.instance,
            },
            timeout=args.timeout,
        )
        emit(summarize(request=request, provider_payload=provider_payload))
        return 0
    except urllib.error.HTTPError as exc:
        emit_http_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except LaunchplaneSafetyError as exc:
        emit_safety_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError):
        emit_provider_unavailable(operation=operation, request=request)
        return 1


def _summarize_product_read(
    *,
    operation: str,
    request: dict[str, object],
    provider_payload: dict[str, Any],
    result_key: str,
    project: Any,
    recommendation: str,
) -> dict[str, object]:
    if any(str(key) not in {"status", "trace_id", result_key} for key in provider_payload):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    status = public_code(provider_payload.get("status"), default="ok")
    payload = base_payload(status=status, operation=operation, request=request)
    payload["result"] = project(provider_payload.get(result_key))
    payload["summary"] = {
        "launchplane_status": status,
        "trace_id": public_trace_id(provider_payload.get("trace_id")),
        "recommendation": recommendation,
    }
    assert_public_safe_shape(payload["summary"])
    return payload


def _project_path_check(value: object) -> dict[str, object]:
    source = _require_dict(value)
    fields = {"product", "path", "state", "blocked_count", "unknown_count", "steps"}
    step_fields = {"step_id", "state", "code", "description", "fix", "record_ids"}
    states = ("clear", "blocked", "unknown")
    fixes = ("none", "code", "grant", "owner_approval", "client_acceptance", "by_hand", "wait")
    if set(source) != fields:
        raise LaunchplaneSafetyError("unsafe_response_shape")
    assert_public_safe_shape(source)
    if source["path"] not in ("testing", "promote") or source["state"] not in states:
        raise LaunchplaneSafetyError("invalid_response")
    steps = source["steps"]
    if not isinstance(steps, list) or not steps or len(steps) > 50:
        raise LaunchplaneSafetyError("invalid_response")
    projected_steps: list[dict[str, object]] = []
    for value in steps:
        step = _require_dict(value)
        if set(step) != step_fields:
            raise LaunchplaneSafetyError("unsafe_response_shape")
        if step["state"] not in states or step["fix"] not in fixes:
            raise LaunchplaneSafetyError("invalid_response")
        if not isinstance(step["code"], str):
            raise LaunchplaneSafetyError("invalid_response")
        record_ids = step["record_ids"]
        if not isinstance(record_ids, list) or len(record_ids) > 50:
            raise LaunchplaneSafetyError("invalid_response")
        projected_steps.append(
            {
                "step_id": public_identifier(step["step_id"]),
                "state": step["state"],
                "code": public_code(step["code"]),
                "description": public_summary_string(step["description"]),
                "fix": step["fix"],
                "record_ids": [public_identifier(record_id) for record_id in record_ids],
            }
        )
    blocked = sum(step["state"] == "blocked" for step in projected_steps)
    unknown = sum(step["state"] == "unknown" for step in projected_steps)
    state = "blocked" if blocked else "unknown" if unknown else "clear"
    for field, count in (("blocked_count", blocked), ("unknown_count", unknown)):
        if type(source[field]) is not int or source[field] != count:
            raise LaunchplaneSafetyError("invalid_response")
    if source["state"] != state:
        raise LaunchplaneSafetyError("invalid_response")
    return {
        "product": public_identifier(source["product"]),
        "path": source["path"],
        "state": state,
        "blocked_count": blocked,
        "unknown_count": unknown,
        "steps": projected_steps,
    }


def summarize_path_check(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    payload = _summarize_product_read(
        operation="path-check",
        request=request,
        provider_payload=provider_payload,
        result_key="check",
        project=_project_path_check,
        recommendation=(
            "Read every blocked or unknown step before proposing the next action; "
            "this read grants no authority."
        ),
    )
    result = _require_dict(payload["result"])
    if result["product"] != request["product"] or result["path"] != request["path"]:
        raise LaunchplaneSafetyError("invalid_response")
    return payload


def summarize_product_environment_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    return _summarize_product_read(
        operation="product-environment-read",
        request=request,
        provider_payload=provider_payload,
        result_key="environment",
        project=_project_product_environment,
        recommendation=(
            "Compare target.artifact and the expected and observed runtime identity "
            "with the build you expect this lane to serve."
        ),
    )


def summarize_product_profile_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    return _summarize_product_read(
        operation="product-profile-read",
        request=request,
        provider_payload=provider_payload,
        result_key="profile",
        project=_project_product_profile,
        recommendation=(
            "production_use prelaunch skips Owner release review; live and unknown "
            "both require it."
        ),
    )


def summarize_product_activity_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    return _summarize_product_read(
        operation="product-activity-read",
        request=request,
        provider_payload=provider_payload,
        result_key="activity",
        project=_project_product_activity,
        recommendation=(
            "Events are newest first; read one lane's current build with "
            "product-environment-read."
        ),
    )


def summarize_preview_history_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    if any(str(key) not in {"status", "trace_id", "preview", "generations"} for key in provider_payload):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    status = public_code(provider_payload.get("status"), default="ok")
    payload = base_payload(status=status, operation="preview-history-read", request=request)
    payload["result"] = _project_preview_history(provider_payload)
    payload["summary"] = {
        "launchplane_status": status,
        "trace_id": public_trace_id(provider_payload.get("trace_id")),
        "recommendation": (
            "Generations are newest first; compare serving_generation_id with the generation "
            "whose anchor_head_sha is the pull request head you expect."
        ),
    }
    assert_public_safe_shape(payload["summary"])
    return payload


def summarize_reconcile_requests_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    if any(str(key) not in {"status", "trace_id", "product", "requests"} for key in provider_payload):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    status = public_code(provider_payload.get("status"), default="ok")
    payload = base_payload(status=status, operation="reconcile-requests-read", request=request)
    payload["result"] = _project_reconcile_requests(provider_payload)
    payload["summary"] = {
        "launchplane_status": status,
        "trace_id": public_trace_id(provider_payload.get("trace_id")),
        "recommendation": (
            "Compare each target's last_plan action, reason and desired_image_digest with "
            "the build you expect; read the preview itself with preview-history-read."
        ),
    }
    assert_public_safe_shape(payload["summary"])
    return payload


def summarize_product_secret_bindings_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    if any(str(key) not in {"status", "trace_id", "product", "bindings"} for key in provider_payload):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    status = public_code(provider_payload.get("status"), default="ok")
    payload = base_payload(
        status=status, operation="product-secret-bindings-read", request=request
    )
    payload["result"] = _project_product_secret_bindings(provider_payload)
    payload["summary"] = {
        "launchplane_status": status,
        "trace_id": public_trace_id(provider_payload.get("trace_id")),
        "recommendation": (
            "Only bindings your access covers are listed. To copy one, put its context, "
            "instance and version_id in a product-config secret entry's copy_from and "
            "dry-run first; Launchplane refuses a source whose class does not allow the lane."
        ),
    }
    assert_public_safe_shape(payload["summary"])
    return payload


def summarize_target_replacement_operation_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    if any(str(key) not in {"status", "trace_id", "operation", "result"} for key in provider_payload):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    status = public_code(provider_payload.get("status"), default="ok")
    payload = base_payload(
        status=status, operation="target-replacement-operation-read", request=request
    )
    payload["result"] = _project_target_replacement_operation(provider_payload)
    payload["summary"] = {
        "launchplane_status": status,
        "trace_id": public_trace_id(provider_payload.get("trace_id")),
        "recommendation": (
            "Read operation.status, phase, error_code, error_description and error_detail_keys "
            "first; the result statuses say "
            "which deploy or verification step failed. Error messages are not returned."
        ),
    }
    assert_public_safe_shape(payload["summary"])
    return payload


TARGET_REPLACEMENT_PLAN_RESPONSE_FIELDS = frozenset(
    {"status", "trace_id", "records", "result", "replayed", "original_trace_id"}
)


def summarize_target_replacement_plan_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    if any(str(key) not in TARGET_REPLACEMENT_PLAN_RESPONSE_FIELDS for key in provider_payload):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    if provider_payload.get("records") not in (None, {}):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    status = public_code(provider_payload.get("status"), default="ok")
    payload = base_payload(status=status, operation="target-replacement-plan-read", request=request)
    payload["result"] = _project_target_replacement_plan(provider_payload.get("result"))
    payload["summary"] = {
        "launchplane_status": status,
        "trace_id": public_trace_id(provider_payload.get("trace_id")),
        "recommendation": (
            "Read plan_status and blocker_codes first; blocker_keys names the env keys each "
            "key-list blocker is about. Blocker, step and warning text is not returned."
        ),
    }
    assert_public_safe_shape(payload["summary"])
    return payload


def execute_target_replacement_plan_read(*, args: argparse.Namespace) -> int:
    """POST the read-only plan route: it builds a plan and writes no record."""
    product = public_identifier(_required_argument(args, "product"))
    instance = public_identifier(_required_argument(args, "instance"))
    for name, value in (("product", product), ("instance", instance)):
        if not PRODUCT_READ_PATH_SEGMENT_RE.fullmatch(value):
            raise ValueError(f"invalid_{name}")
    operation = "target-replacement-plan-read"
    request: dict[str, object] = {
        "product": product,
        "instance": instance,
        "payload_source": "operator_argument",
    }
    body: dict[str, object] = {
        "schema_version": 1,
        "product": product,
        "replacement": {"product": product, "instance": instance},
    }
    settings = prepare_operator_settings(args=args, operation=operation, request=request)
    if settings is None:
        return 2
    try:
        provider_payload = request_launchplane(
            service_url=settings["service_url"],
            path=helper_command_path(operation),
            settings=settings,
            body=body,
            timeout=args.timeout,
        )
        emit(summarize_target_replacement_plan_read(request=request, provider_payload=provider_payload))
        return 0
    except urllib.error.HTTPError as exc:
        emit_http_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except LaunchplaneSafetyError as exc:
        emit_safety_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError):
        emit_provider_unavailable(operation=operation, request=request)
        return 1


PRODUCT_READ_SUMMARIZERS = {
    "path-check": summarize_path_check,
    "product-environment-read": summarize_product_environment_read,
    "product-activity-read": summarize_product_activity_read,
    "product-profile-read": summarize_product_profile_read,
    "preview-history-read": summarize_preview_history_read,
    "reconcile-requests-read": summarize_reconcile_requests_read,
    "product-secret-bindings-read": summarize_product_secret_bindings_read,
    "target-replacement-operation-read": summarize_target_replacement_operation_read,
}


def execute_product_read(
    *,
    args: argparse.Namespace,
    operation: str,
    request: dict[str, object],
    path: str,
    query: dict[str, str] | None = None,
) -> int:
    summarize = PRODUCT_READ_SUMMARIZERS[operation]
    settings = prepare_operator_settings(args=args, operation=operation, request=request)
    if settings is None:
        return 2
    try:
        provider_payload = request_launchplane_read(
            service_url=settings["service_url"],
            path=path,
            settings=settings,
            query=query or {},
            timeout=args.timeout,
        )
        emit(summarize(request=request, provider_payload=provider_payload))
        return 0
    except urllib.error.HTTPError as exc:
        emit_http_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except LaunchplaneSafetyError as exc:
        emit_safety_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError):
        emit_provider_unavailable(operation=operation, request=request)
        return 1


def _load_reviewed_evidence(
    args: argparse.Namespace,
    *,
    operation: str,
    expected_digest: str,
    digest_field: str = "plan_sha256",
    evidence_status: str = "accepted",
    result_status: str | None = "ok",
    dry_run_mode: str = "dry-run",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The saved dry-run output and its result, when it is this operation's accepted plan with the digest."""
    evidence_path = str(getattr(args, "dry_run_evidence_file", "") or "").strip()
    if not evidence_path:
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    try:
        evidence = read_payload_file(evidence_path)
    except ValueError:
        raise ValueError("reviewed_dry_run_not_apply_eligible") from None
    result = evidence.get("result")
    if (
        not isinstance(result, dict)
        or not isinstance(evidence.get("request"), dict)
        or evidence.get("operation") != operation
        or evidence.get("status") != evidence_status
        or (result_status is not None and result.get("status") != result_status)
        or result.get("mode") != dry_run_mode
        or result.get(digest_field) != expected_digest
    ):
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    return evidence, result


def _load_reviewed_plan_evidence(
    args: argparse.Namespace, *, operation: str, expected_plan_digest: str
) -> dict[str, Any]:
    """The saved dry-run result, when it is this operation's accepted plan with the digest."""
    return _load_reviewed_evidence(
        args, operation=operation, expected_digest=expected_plan_digest
    )[1]


def _reviewed_apply_digest(args: argparse.Namespace) -> str:
    _require_idempotency(args)
    if not args.reviewed_dry_run:
        raise ValueError("reviewed_dry_run_required")
    expected_plan_digest = args.expected_plan_digest.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_plan_digest):
        raise ValueError("invalid_expected_plan_digest")
    return expected_plan_digest


def _required_argument(args: argparse.Namespace, field: str) -> str:
    value = getattr(args, field, "")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}_required")
    return value.strip()


def testing_hold_body(args: argparse.Namespace, *, mode: str) -> dict[str, object]:
    body: dict[str, object] = {
        "schema_version": 1,
        "product": _required_argument(args, "product"),
        # The service stores lanes lower-cased; send what it will compare against.
        "context": _required_argument(args, "context").lower(),
        "instance": _required_argument(args, "instance").lower(),
        "mode": mode,
        "hold": bool(args.hold),
        "reason": _required_argument(args, "reason"),
    }
    if mode == "apply":
        expected_plan_digest = _reviewed_apply_digest(args)
        result = _load_reviewed_plan_evidence(
            args, operation="testing-hold-dry-run", expected_plan_digest=expected_plan_digest
        )
        if (
            result.get("product") != body["product"]
            or result.get("context") != body["context"]
            or result.get("instance") != body["instance"]
            # A lift's reason is not in the plan digest, so bind direction and reason here.
            or (result.get("after") is not None) != body["hold"]
            or result.get("reason") != " ".join(str(body["reason"]).split())
        ):
            raise ValueError("reviewed_dry_run_not_apply_eligible")
        body["reviewed_plan_sha256"] = expected_plan_digest
    return body


def product_repository_identity_body(
    args: argparse.Namespace, *, mode: str
) -> dict[str, object]:
    body: dict[str, object] = {
        "schema_version": 1,
        "product": _required_argument(args, "product"),
        "mode": mode,
        "reason": _required_argument(args, "reason"),
    }
    if mode == "apply":
        expected_plan_digest = _reviewed_apply_digest(args)
        result = _load_reviewed_plan_evidence(
            args,
            operation="product-repository-identity-dry-run",
            expected_plan_digest=expected_plan_digest,
        )
        if result.get("product") != body["product"]:
            raise ValueError("reviewed_dry_run_not_apply_eligible")
        body["reviewed_plan_sha256"] = expected_plan_digest
    return body


def odoo_addon_settings_body(args: argparse.Namespace, *, mode: str) -> dict[str, object]:
    body = read_payload_file(args.payload_file)
    if any(str(key) not in ODOO_ADDON_SETTINGS_PAYLOAD_FIELDS for key in body):
        raise ValueError("unsupported_payload_field")
    if body.get("schema_version") != 1:
        raise ValueError("schema_version_required")
    identity: dict[str, str] = {}
    for field in ("product", "context", "instance", "reason"):
        value = body.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field}_required")
        identity[field] = value.strip()
    if body.get("addon", "shopify") != "shopify":
        raise ValueError("unsupported_addon")
    shopify = body.get("shopify")
    if not isinstance(shopify, dict):
        raise ValueError("shopify_settings_required")
    # Only binding references are accepted for secret settings; plaintext fields are refused.
    if any(str(key) not in ODOO_ADDON_SETTINGS_SHOPIFY_FIELDS for key in shopify):
        raise ValueError("unsupported_shopify_field")
    if not isinstance(shopify.get("test_store"), bool):
        raise ValueError("test_store_boolean_required")
    body["mode"] = mode
    if mode == "apply":
        _require_idempotency(args)
        if not args.reviewed_dry_run:
            raise ValueError("reviewed_dry_run_required")
        expected_plan_digest = args.expected_plan_digest.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_plan_digest):
            raise ValueError("invalid_expected_plan_digest")
        _require_apply_eligible_odoo_addon_settings_dry_run(
            args,
            expected_plan_digest=expected_plan_digest,
            expected_product=identity["product"],
            expected_context=identity["context"].lower(),
            expected_instance=identity["instance"].lower(),
        )
        body["reviewed_plan_sha256"] = expected_plan_digest
    return body


def generic_web_deploy_recovery_body(
    args: argparse.Namespace, *, mode: str
) -> dict[str, object]:
    body = read_payload_file(args.payload_file)
    _require_idempotency(args)
    if body.get("schema_version") != 1:
        raise ValueError("schema_version_required")
    product = body.get("product")
    instance = body.get("instance")
    reason = body.get("reason")
    original_deploy = body.get("original_deploy")
    if not isinstance(product, str) or not product.strip():
        raise ValueError("product_required")
    if not isinstance(instance, str) or not instance.strip():
        raise ValueError("instance_required")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason_required")
    if not isinstance(original_deploy, dict):
        raise ValueError("original_deploy_required")
    deploy_request = original_deploy.get("deploy")
    if (
        str(original_deploy.get("product") or "").strip() != product.strip()
        or not isinstance(deploy_request, dict)
        or str(deploy_request.get("instance") or "").strip() != instance.strip()
    ):
        raise ValueError("original_deploy_identity_mismatch")
    if mode == "apply":
        if not args.reviewed_dry_run:
            raise ValueError("reviewed_dry_run_required")
        expected_recovery_digest = args.expected_recovery_digest.strip().lower()
        if not expected_recovery_digest:
            raise ValueError("expected_recovery_digest_required")
        if len(expected_recovery_digest) != 64 or any(
            character not in "0123456789abcdef" for character in expected_recovery_digest
        ):
            raise ValueError("invalid_expected_recovery_digest")
        raw_payload_recovery_digest = body.get("expected_recovery_digest")
        if raw_payload_recovery_digest is not None and not isinstance(
            raw_payload_recovery_digest, str
        ):
            raise ValueError("invalid_expected_recovery_digest")
        payload_recovery_digest = (
            raw_payload_recovery_digest.strip().lower()
            if isinstance(raw_payload_recovery_digest, str)
            else ""
        )
        if payload_recovery_digest and payload_recovery_digest != expected_recovery_digest:
            raise ValueError("recovery_digest_mismatch")
        _require_apply_eligible_recovery_dry_run(
            args,
            expected_recovery_digest=expected_recovery_digest,
            expected_product=product.strip(),
            expected_instance=instance.strip(),
        )
        body["expected_recovery_digest"] = expected_recovery_digest
    return body


def product_config_preflight_body(args: argparse.Namespace) -> dict[str, object]:
    secret_bindings = tuple(args.secret_binding or ())
    destination: dict[str, object] | None = None
    if secret_bindings:
        destination_instance = (args.destination_instance or args.instance or "").strip()
        if not destination_instance:
            raise ValueError("destination_instance_required")
        destination = {
            "kind": "runtime_environment",
            "context": (args.destination_context or args.context).strip(),
            "instance": destination_instance,
        }
    body: dict[str, object] = {
        "schema_version": 1,
        "intent": "product_config_apply",
        "mode": "dry_run",
        "product": args.product,
        "context": args.context,
        "source_url": args.source_url,
        "reason": args.reason,
        "secret_bindings": list(secret_bindings),
    }
    if destination is not None:
        body["destination"] = destination
    if args.idempotency_key:
        body["idempotency_key"] = args.idempotency_key
    return body


def _validate_product_config_secret_copies(body: dict[str, object]) -> None:
    """A copy entry names its source lane and reviewed version, and carries no value."""
    secrets = body.get("secrets")
    if not isinstance(secrets, list):
        return
    for secret in secrets:
        if not isinstance(secret, dict) or secret.get("copy_from") is None:
            continue
        if secret.get("value") is not None:
            raise ValueError("secret_copy_with_value")
        copy_from = secret["copy_from"]
        if (
            not isinstance(copy_from, dict)
            or set(copy_from) != set(SECRET_COPY_FROM_FIELDS)
            or not all(
                isinstance(copy_from[key], str) and copy_from[key].strip()
                for key in SECRET_COPY_FROM_FIELDS
            )
        ):
            raise ValueError("invalid_secret_copy_from")


def product_config_payload_body(args: argparse.Namespace, *, mode: str) -> dict[str, object]:
    body = read_payload_file(args.payload_file)
    _validate_product_config_secret_copies(body)
    body["mode"] = mode
    if mode == "apply":
        _require_idempotency(args)
        if not args.reviewed_dry_run:
            raise ValueError("reviewed_dry_run_required")
        reason = body.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason_required")
    return body


def _validate_expected_config_removal(item: dict[str, object], *, identity_key: str) -> None:
    """A removal names one declared identity with plain strings and nothing else."""
    if not all(isinstance(value, str) for value in item.values()):
        raise ValueError("invalid_expected_config_removal")
    if not str(item.get(identity_key, "")).strip():
        raise ValueError("invalid_expected_config_removal")
    if "integration" in item and not str(item["integration"]).strip():
        raise ValueError("invalid_expected_config_removal")
    if str(item.get("instance", "")).strip() and not str(item.get("context", "")).strip():
        raise ValueError("invalid_expected_config_removal")


def product_expected_config_payload_body(args: argparse.Namespace, *, mode: str) -> dict[str, object]:
    body = read_payload_file(args.payload_file)
    if set(body) - {
        "schema_version", "product", "mode", "reason", "source_label",
        "runtime_environment_keys", "managed_secret_bindings",
        "remove_runtime_environment_keys", "remove_managed_secret_bindings",
    }:
        raise ValueError("invalid_expected_config_payload")
    if body.get("schema_version") != 1:
        raise ValueError("schema_version_required")
    for key in ("product", "reason"):
        value = body.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key}_required")
    count = 0
    for kind, allowed in (
        ("runtime_environment_keys", {"key", "context", "instance"}),
        ("managed_secret_bindings", {"integration", "binding_key", "context", "instance", "owner_input"}),
        ("remove_runtime_environment_keys", {"key", "context", "instance"}),
        ("remove_managed_secret_bindings", {"integration", "binding_key", "context", "instance"}),
    ):
        requirements = body.get(kind, [])
        if not isinstance(requirements, list):
            raise ValueError("invalid_expected_config_payload")
        for item in requirements:
            if not isinstance(item, dict) or set(item) - allowed:
                raise ValueError("invalid_expected_config_payload")
            if kind.startswith("remove_"):
                _validate_expected_config_removal(item, identity_key="key" if kind == "remove_runtime_environment_keys" else "binding_key")
            owner_input = item.get("owner_input")
            if owner_input is not None and (
                not isinstance(owner_input, dict)
                or set(owner_input) - {"label", "instructions"}
                or not isinstance(owner_input.get("label"), str)
                or not owner_input["label"].strip()
                or not isinstance(owner_input.get("instructions", ""), str)
                or not isinstance(item.get("context"), str)
                or not item["context"].strip()
            ):
                raise ValueError("invalid_owner_input_metadata")
        count += len(requirements)
    if not count:
        raise ValueError("expected_config_requirements_required")
    body["mode"] = mode
    if mode == "apply":
        _require_idempotency(args)
        if not args.reviewed_dry_run:
            raise ValueError("reviewed_dry_run_required")
        try:
            evidence = read_payload_file(args.dry_run_evidence_file)
        except ValueError:
            raise ValueError("reviewed_dry_run_not_apply_eligible") from None
        request = evidence.get("request")
        result = evidence.get("result")
        if (
            evidence.get("operation") != "product-expected-config-dry-run"
            or evidence.get("status") not in {"ok", "accepted"}
            or not isinstance(request, dict)
            or request.get("payload_digest") != metadata_review_digest(body)
            or not isinstance(result, dict)
            or result.get("status") != "ok"
            or result.get("mode") != "dry-run"
        ):
            raise ValueError("reviewed_dry_run_not_apply_eligible")
    return body


def change_impact_policy_payload_body(
    args: argparse.Namespace, *, mode: str
) -> dict[str, object]:
    body = read_payload_file(args.payload_file)
    body["mode"] = mode
    record = body.get("record")
    if not isinstance(record, dict):
        raise ValueError("record_required")
    if mode == "apply":
        _require_idempotency(args)
        if not args.reviewed_dry_run:
            raise ValueError("reviewed_dry_run_required")
        reason = record.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason_required")
        expected_policy_digest = args.expected_policy_digest.strip().lower()
        payload_policy_digest = str(record.get("policy_digest") or "").strip().lower()
        if not expected_policy_digest:
            raise ValueError("expected_policy_digest_required")
        if len(expected_policy_digest) != 64 or any(
            character not in "0123456789abcdef" for character in expected_policy_digest
        ):
            raise ValueError("invalid_expected_policy_digest")
        if payload_policy_digest and payload_policy_digest != expected_policy_digest:
            raise ValueError("policy_digest_mismatch")
        record["policy_digest"] = expected_policy_digest
    return body


def _required_lower_sha256(value: object, *, code: str) -> str:
    if not isinstance(value, str):
        raise ValueError(code)
    normalized = str(value)
    if normalized != normalized.strip().lower():
        raise ValueError(code)
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(code)
    return normalized


def _require_merge_train_policy_dry_run_evidence(
    args: argparse.Namespace,
    *,
    expected_current_policy_digest: str,
    expected_new_policy_digest: str,
    expected_record_id: str,
    expected_record_status: str,
) -> None:
    evidence_path = str(getattr(args, "dry_run_evidence_file", "") or "").strip()
    if not evidence_path:
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    try:
        evidence = read_payload_file(evidence_path)
    except ValueError:
        raise ValueError("reviewed_dry_run_not_apply_eligible") from None
    expected_top_level = {
        "schema_version",
        "status",
        "provider",
        "operation",
        "generated_at",
        "request",
        "summary",
        "records",
        "result",
        "warnings",
    }
    result = evidence.get("result")
    evidence_request = evidence.get("request")
    if (
        set(map(str, evidence)) != expected_top_level
        or evidence.get("schema_version") != SCHEMA_VERSION
        or evidence.get("status") != "accepted"
        or evidence.get("provider") != PROVIDER
        or evidence.get("operation") != "merge-train-policy-import-dry-run"
        or evidence.get("warnings") != []
        or evidence.get("records") != {}
        or not isinstance(evidence.get("summary"), dict)
        or not isinstance(evidence_request, dict)
        or set(map(str, evidence_request))
        != {
            "mode",
            "payload_source",
            "expected_current_policy_sha256",
            "candidate_policy_sha256",
        }
        or evidence_request.get("mode") != "dry_run"
        or evidence_request.get("payload_source") != "private_file"
        or evidence_request.get("expected_current_policy_sha256")
        != expected_current_policy_digest
        or evidence_request.get("candidate_policy_sha256")
        != expected_new_policy_digest
        or not isinstance(result, dict)
        or set(map(str, result)) != {"mode", "current_policy", "candidate"}
        or result.get("mode") != "dry_run"
    ):
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    current_policy = result.get("current_policy")
    candidate = result.get("candidate")
    candidate_keys = set(map(str, candidate)) if isinstance(candidate, dict) else set()
    if (
        not isinstance(current_policy, dict)
        or not isinstance(candidate, dict)
        or set(map(str, current_policy))
        != {"record_id", "updated_at", "policy_sha256", "target_count", "trace_id"}
        or candidate_keys
        not in (
            {"record_id", "status", "updated_at", "policy_sha256", "target_count"},
            {
                "record_id",
                "status",
                "updated_at",
                "policy_sha256",
                "target_count",
                "replayed",
            },
        )
        or ("replayed" in candidate and not isinstance(candidate.get("replayed"), bool))
        or current_policy.get("policy_sha256") != expected_current_policy_digest
        or candidate.get("policy_sha256") != expected_new_policy_digest
        or candidate.get("record_id") != expected_record_id
        or candidate.get("status") != expected_record_status
        or not isinstance(current_policy.get("target_count"), int)
        or isinstance(current_policy.get("target_count"), bool)
        or current_policy.get("target_count", -1) < 0
        or not isinstance(candidate.get("target_count"), int)
        or isinstance(candidate.get("target_count"), bool)
        or candidate.get("target_count", 0) < 1
    ):
        raise ValueError("reviewed_dry_run_not_apply_eligible")


def merge_train_policy_import_body(
    args: argparse.Namespace, *, mode: str
) -> tuple[dict[str, object], str, str]:
    body = read_payload_file(args.payload_file)
    if set(map(str, body)) != MERGE_TRAIN_POLICY_IMPORT_ENVELOPE_FIELDS:
        raise ValueError("invalid_payload_shape")
    if body.get("schema_version") != 1:
        raise ValueError("schema_version_required")
    if body.get("product") != "launchplane":
        raise ValueError("invalid_product")
    if body.get("mode") != mode:
        raise ValueError("mode_mismatch")
    reason = body.get("reason")
    if not isinstance(reason, str):
        raise ValueError("reason_required")
    record = body.get("record")
    if not isinstance(record, dict) or set(map(str, record)) != MERGE_TRAIN_POLICY_RECORD_FIELDS:
        raise ValueError("invalid_record_shape")
    if record.get("schema_version") != 1 or not isinstance(record.get("policy"), dict):
        raise ValueError("invalid_record")
    record_id = record.get("record_id")
    source = record.get("source")
    updated_at = record.get("updated_at")
    if not isinstance(record_id, str) or not record_id.strip():
        raise ValueError("record_id_required")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source_required")
    if not isinstance(updated_at, str) or not updated_at.strip():
        raise ValueError("updated_at_required")
    if record.get("status") not in {"active", "superseded"}:
        raise ValueError("invalid_record_status")
    candidate_digest = _required_lower_sha256(
        record.get("policy_sha256"), code="invalid_policy_digest"
    )
    if not record_id.endswith(f"-{candidate_digest[:12]}"):
        raise ValueError("record_id_digest_mismatch")
    expected_current_digest = _required_lower_sha256(
        args.expected_current_policy_digest,
        code="invalid_expected_current_policy_digest",
    )
    if mode == "apply":
        _require_idempotency(args)
        if not args.reviewed_dry_run:
            raise ValueError("reviewed_dry_run_required")
        if not reason.strip():
            raise ValueError("reason_required")
        expected_new_digest = _required_lower_sha256(
            args.expected_new_policy_digest,
            code="invalid_expected_new_policy_digest",
        )
        if expected_new_digest != candidate_digest:
            raise ValueError("new_policy_digest_mismatch")
        _require_merge_train_policy_dry_run_evidence(
            args,
            expected_current_policy_digest=expected_current_digest,
            expected_new_policy_digest=expected_new_digest,
            expected_record_id=record_id,
            expected_record_status=str(record["status"]),
        )
    return body, expected_current_digest, candidate_digest


def merge_train_controller_body(args: argparse.Namespace) -> dict[str, object]:
    if args.mutate:
        _require_idempotency(args)
    return {
        "schema_version": 1,
        "repository": args.repo,
        "base_branch": args.base_branch,
        "mutate": bool(args.mutate),
    }


def preview_feedback_remediation_body(args: argparse.Namespace) -> dict[str, object]:
    _require_idempotency(args)
    if args.mode == "apply" and not args.reviewed_dry_run:
        raise ValueError("reviewed_dry_run_required")
    pull_request_url = args.pull_request_url.strip()
    confirmation = ""
    if args.mode == "apply":
        confirmation = (
            f"remediate preview feedback {pull_request_url} "
            f"to {args.terminal_status}"
        )
    return {
        "schema_version": 1,
        "mode": args.mode,
        "product": args.product,
        "context": args.context,
        "repository": args.repository,
        "pull_request_url": pull_request_url,
        "terminal_status": args.terminal_status,
        "reason": args.reason,
        "related_issue": args.related_issue,
        "confirmation": confirmation,
    }


def prepare_operator_settings(
    *,
    args: argparse.Namespace,
    operation: str,
    request: dict[str, object],
) -> dict[str, str] | None:
    try:
        settings = resolve_settings(args)
    except ValueError:
        emit(
            unavailable_payload(
                operation=operation,
                request=request,
                status="invalid",
                code="invalid_config",
                message="Launchplane operator config is invalid.",
            )
        )
        return None
    if settings["service_url"]:
        try:
            validate_service_url(settings["service_url"])
        except LaunchplaneSafetyError as exc:
            emit(
                unavailable_payload(
                    operation=operation,
                    request=request,
                    status="invalid",
                    code=exc.code,
                    message=operator_config_message(exc.code),
                )
            )
            return None
    if not settings["service_url"] or not settings["token"]:
        classification = classify_operator_config(
            service_url_present=bool(settings["service_url"]),
            token_present=bool(settings["token"]),
            public_url_hint_present=bool(settings["public_url_hint_sources"]),
        )
        emit(
            no_context_payload(
                operation=operation,
                request=request,
                code=classification,
                message=operator_config_message(classification),
                recommendation=operator_config_recommendation(classification),
            )
        )
        return None
    return settings


def execute_post(
    *,
    args: argparse.Namespace,
    operation: str,
    path: str,
    request: dict[str, object],
    body: dict[str, object],
) -> int:
    settings = prepare_operator_settings(
        args=args, operation=operation, request=request
    )
    if settings is None:
        return 2
    try:
        provider_payload = request_launchplane(
            service_url=settings["service_url"],
            path=path,
            settings=settings,
            body=body,
            timeout=args.timeout,
            idempotency_key=args.idempotency_key,
        )
        try:
            emit(
                summarize_success(
                    operation=operation,
                    request=request,
                    provider_payload=provider_payload,
                )
            )
        except LaunchplaneSafetyError:
            if operation not in {
                "odoo-addon-settings-apply",
                "integration-allowances-apply",
                "testing-hold-apply",
                "product-repository-identity-apply",
                "change-impact-policy-apply",
                "generic-web-deploy-recovery-apply",
                "repository-inventory-apply",
                "product-expected-config-apply",
            }:
                raise
            try:
                trace_id = public_trace_id(provider_payload.get("trace_id"))
            except LaunchplaneSafetyError:
                trace_id = ""
            payload = base_payload(
                status="accepted_unverified", operation=operation, request=request
            )
            recovery_apply = operation == "generic-web-deploy-recovery-apply"
            inventory_apply = operation == "repository-inventory-apply"
            payload["summary"] = {
                "trace_id": trace_id,
                "recommendation": (
                    "Launchplane accepted the recovery apply, but the response could not be "
                    "verified locally. Inspect the original deploy reservation and normal deploy "
                    "evidence before retrying."
                    if recovery_apply
                    else (
                        "Launchplane accepted the inventory apply, but the response could not be "
                        "verified locally. Read back the current inventory record before retrying."
                        if inventory_apply
                        else (
                            "Launchplane accepted the apply request, but the response could not be "
                            "verified locally. Read back the affected record before retrying."
                        )
                    )
                ),
            }
            payload["warnings"] = [
                warning(
                    "apply_response_unverified",
                    (
                        "The recovery apply response was not safe to project; do not retry before "
                        "reservation verification."
                        if recovery_apply
                        else (
                            "The inventory apply response was not safe to project; do not retry "
                            "before read-back."
                            if inventory_apply
                            else (
                                "The apply response was not safe to project; do not retry before "
                                "read-back."
                            )
                        )
                    ),
                )
            ]
            emit(payload)
        return 0
    except urllib.error.HTTPError as exc:
        emit_http_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except LaunchplaneSafetyError as exc:
        emit_safety_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError):
        emit_provider_unavailable(operation=operation, request=request)
        return 1
    except (ValueError, json.JSONDecodeError):
        emit_invalid_response(operation=operation, request=request)
        return 1


def execute_merge_train_policy_import(
    *,
    args: argparse.Namespace,
    operation: str,
    request: dict[str, object],
    body: dict[str, object],
    expected_current_policy_digest: str,
    expected_candidate_policy_digest: str,
) -> int:
    settings = prepare_operator_settings(
        args=args, operation=operation, request=request
    )
    if settings is None:
        return 2
    record = body["record"]
    if not isinstance(record, dict):
        raise AssertionError("validated merge-train policy record is missing")
    post_attempted = False
    try:
        policy_targets_payload = request_launchplane_read(
            service_url=settings["service_url"],
            path=internal_helper_path("merge-train-policy-targets-read"),
            settings=settings,
            query={},
            timeout=args.timeout,
        )
        current_policy = _project_merge_train_policy_targets(policy_targets_payload)
        if current_policy["policy_sha256"] != expected_current_policy_digest:
            payload = base_payload(
                status="stale", operation=operation, request=request
            )
            payload["result"] = {"current_policy": current_policy}
            payload["summary"] = {
                "error_code": "current_policy_digest_mismatch",
                "expected_current_policy_sha256": expected_current_policy_digest,
                "observed_current_policy_sha256": current_policy["policy_sha256"],
                "recommendation": "Stop before import and rebuild reviewed evidence from the current active policy.",
            }
            assert_public_safe_shape(payload["result"])
            assert_public_safe_shape(payload["summary"])
            emit(payload)
            return 1
        post_attempted = True
        provider_payload = request_launchplane(
            service_url=settings["service_url"],
            path=helper_command_path(operation),
            settings=settings,
            body=body,
            timeout=args.timeout,
            idempotency_key=args.idempotency_key,
        )
        try:
            emit(
                summarize_merge_train_policy_import_success(
                    operation=operation,
                    request=request,
                    current_policy=current_policy,
                    provider_payload=provider_payload,
                    expected_record_id=str(record["record_id"]),
                    expected_policy_sha256=expected_candidate_policy_digest,
                )
            )
        except LaunchplaneSafetyError:
            if operation != "merge-train-policy-import-apply":
                raise
            try:
                trace_id = public_trace_id(provider_payload.get("trace_id"))
            except LaunchplaneSafetyError:
                trace_id = ""
            payload = base_payload(
                status="accepted_unverified", operation=operation, request=request
            )
            payload["result"] = {"current_policy": current_policy}
            payload["summary"] = {
                "trace_id": trace_id,
                "recommendation": "Launchplane accepted the import apply, but its response could not be verified locally. Read back the active merge-train policy before retrying.",
            }
            payload["warnings"] = [
                warning(
                    "apply_response_unverified",
                    "The merge-train policy apply response was not safe to project; do not retry before read-back.",
                )
            ]
            emit(payload)
        return 0
    except urllib.error.HTTPError as exc:
        emit_http_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except LaunchplaneSafetyError as exc:
        if (
            operation == "merge-train-policy-import-apply"
            and post_attempted
            and exc.code == "unsafe_redirect"
        ):
            payload = base_payload(
                status="outcome_unknown", operation=operation, request=request
            )
            payload["summary"] = {
                "recommendation": "The apply request outcome is unknown because Launchplane redirected after POST began. Read back the active merge-train policy before any retry.",
            }
            payload["warnings"] = [
                warning(
                    "apply_outcome_unknown",
                    "Do not retry this merge-train policy import until active-policy read-back resolves the outcome.",
                )
            ]
            emit(payload)
            return 1
        emit_safety_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError):
        if operation == "merge-train-policy-import-apply" and post_attempted:
            payload = base_payload(
                status="outcome_unknown", operation=operation, request=request
            )
            payload["summary"] = {
                "recommendation": "The apply request outcome is unknown because transport failed after POST began. Read back the active merge-train policy before any retry.",
            }
            payload["warnings"] = [
                warning(
                    "apply_outcome_unknown",
                    "Do not retry this merge-train policy import until active-policy read-back resolves the outcome.",
                )
            ]
            emit(payload)
            return 1
        emit_provider_unavailable(operation=operation, request=request)
        return 1
    except (ValueError, json.JSONDecodeError):
        if operation == "merge-train-policy-import-apply" and post_attempted:
            payload = base_payload(
                status="accepted_unverified", operation=operation, request=request
            )
            payload["summary"] = {
                "recommendation": "Launchplane returned an unreadable apply response after accepting the HTTP exchange. Read back the active merge-train policy before retrying.",
            }
            payload["warnings"] = [
                warning(
                    "apply_response_unverified",
                    "Do not retry this merge-train policy import until active-policy read-back resolves the outcome.",
                )
            ]
            emit(payload)
            return 0
        emit_invalid_response(operation=operation, request=request)
        return 1


def execute_change_impact_policy_read(
    *, args: argparse.Namespace, request: dict[str, object]
) -> int:
    operation = "change-impact-policy-read"
    settings = prepare_operator_settings(
        args=args, operation=operation, request=request
    )
    if settings is None:
        return 2
    try:
        provider_payload = request_launchplane_read(
            service_url=settings["service_url"],
            path=helper_command_path(operation),
            settings=settings,
            query={"repository_id": args.repository_id},
            timeout=args.timeout,
        )
        emit(summarize_change_impact_policy_read(request=request, provider_payload=provider_payload))
        return 0
    except urllib.error.HTTPError as exc:
        emit_http_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except LaunchplaneSafetyError as exc:
        emit_safety_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError):
        emit_provider_unavailable(operation=operation, request=request)
        return 1
    except (ValueError, json.JSONDecodeError):
        emit_invalid_response(operation=operation, request=request)
        return 1


def execute_repository_inventory_read(
    *, args: argparse.Namespace, request: dict[str, object]
) -> int:
    operation = "repository-inventory-read"
    settings = prepare_operator_settings(
        args=args, operation=operation, request=request
    )
    if settings is None:
        return 2
    try:
        provider_payload = request_launchplane_read(
            service_url=settings["service_url"],
            path=helper_command_path(operation),
            settings=settings,
            query={"repository_id": args.repository_id},
            timeout=args.timeout,
        )
        emit(
            summarize_repository_inventory_read(
                request=request, provider_payload=provider_payload
            )
        )
        return 0
    except urllib.error.HTTPError as exc:
        emit_http_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except LaunchplaneSafetyError as exc:
        emit_safety_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError):
        emit_provider_unavailable(operation=operation, request=request)
        return 1
    except (ValueError, json.JSONDecodeError):
        emit_invalid_response(operation=operation, request=request)
        return 1


# Lane setup: a product's Client, production backup authority, and a lane's Dokploy
# compose target. Launchplane binds only the backup-authority apply to its dry-run, so
# for the others the helper hashes what the reviewer saw, checks the record has not
# moved before applying, and compares the applied result and a read-back with it.

PRODUCT_OWNER_PLAN_FIELDS = {
    "status",
    "mode",
    "product",
    "operation",
    "resolved_github_login",
    "resolved_github_id",
    "owner_before",
    "owner_after",
    "changed",
    "applied",
    "reason",
    "source_label",
    "profile_updated_at_before",
    "profile_updated_at_after",
}
PRODUCT_OWNER_OPERATIONS = {"set", "clear", "unchanged"}
PRODUCT_OWNER_IDENTITY_FIELDS = {"github_login", "github_id", "review_label"}
PRODUCT_IMAGE_REPOSITORY_PLAN_FIELDS = {
    "status",
    "mode",
    "product",
    "repository",
    "image_repository_before",
    "image_repository_after",
    "changed",
    "applied",
    "lanes",
    "reason",
    "source_label",
    "profile_updated_at_before",
    "profile_updated_at_after",
}
PRODUCT_IMAGE_REPOSITORY_LANE_FIELDS = {
    "instance",
    "context",
    "current_artifact_id",
    "in_new_repository",
}
PRODUCT_IMAGE_REPOSITORY_DIGEST_FIELDS = (
    "product",
    "repository",
    "image_repository_before",
    "image_repository_after",
    "reason",
)
# Launchplane accepts only an untagged lowercase GHCR package; refuse anything else locally.
GHCR_IMAGE_REPOSITORY_RE = re.compile(r"^ghcr\.io/[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$")
# What a profile may hold today, before the move: a lowercase registry path, never a URL.
IMAGE_REPOSITORY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(?::[0-9]{1,5})?(?:/[a-z0-9][a-z0-9._-]*)+$")
PRODUCT_IMAGE_REPOSITORY_MAX_LANES = 64
GITHUB_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
PRODUCTION_BACKUP_AUTHORITY_PAYLOAD_FIELDS = {
    "schema_version",
    "policy",
    "targets",
    "expected_current_policy_record_id",
    "expected_current_target_record_ids",
}
PRODUCTION_BACKUP_AUTHORITY_RESULT_FIELDS = {
    "schema_version",
    "mode",
    "status",
    "authority_digest",
    "policy",
    "targets",
}
PRODUCTION_BACKUP_AUTHORITY_MODES = {"dry_run", "apply"}
PRODUCTION_BACKUP_AUTHORITY_STATUSES = {"would_apply", "applied", "replayed"}
PRODUCTION_BACKUP_POLICY_SUMMARY_FIELDS = {
    "policy_id",
    "record_id",
    "policy_revision",
    "status",
    "promotion_action",
    "source_target_id",
    "destination_target_id",
    "effective_at",
    "review_after",
}
PRODUCTION_BACKUP_TARGET_SUMMARY_FIELDS = {
    "target_id",
    "record_id",
    "target_revision",
    "status",
    "provider_type",
    "destination_kind",
    "effective_at",
    "review_after",
}
PRODUCTION_BACKUP_RECORD_STATUSES = {"active", "superseded", "retired"}
PRODUCTION_BACKUP_DESTINATION_KINDS = {"proxmox_guest", "proxmox_storage"}
PRODUCTION_BACKUP_AUTHORITY_READ_FIELDS = {
    "schema_version",
    "product",
    "context",
    "instance",
    "promotion_action",
    "state",
    "ready",
    "summary",
    "reason_codes",
    "policy",
    "targets",
    "generated_at",
}
PRODUCTION_BACKUP_AUTHORITY_STATES = {"ready", "missing", "invalid", "stale", "retired"}
PRODUCTION_BACKUP_MAX_TARGETS = 20
DOKPLOY_COMPOSE_PAYLOAD_FIELDS = {
    "schema_version",
    "context",
    "instance",
    "target_name",
    "server_id",
    "project_id",
    "project_name",
    "project_description",
    "environment_id",
    "environment_name",
    "environment_description",
    "app_name",
    "description",
    "source_git_ref",
    "source_type",
    "compose_path",
    "custom_git_branch",
    "healthcheck_path",
    "domains",
    "runtime_port",
    "deploy_timeout_seconds",
    "reason",
}
DOKPLOY_COMPOSE_SOURCE_PAYLOAD_FIELDS = {
    "schema_version", "context", "instance", "custom_git_branch", "compose_path", "reason",
}
DOKPLOY_COMPOSE_SETUP_RESULT_FIELDS = {
    "mode",
    "operation",
    "context",
    "instance",
    "applied",
    "route_domain_ids",
    "reason",
    "setup",
}
# Launchplane's typed confirmation for a target-setup apply; the reviewed dry-run
# evidence and plan digest are what the helper checks before sending it.
DOKPLOY_TARGET_SETUP_CONFIRMATION = "APPLY DOKPLOY TARGET SETUP"


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _positive_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise LaunchplaneSafetyError("invalid_response")
    return cast(int, value)


def _project_owner_identity(value: object) -> dict[str, str]:
    source = {} if value is None else _require_dict(value)
    if any(str(key) not in PRODUCT_OWNER_IDENTITY_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    login = source.get("github_login") or ""
    github_id = source.get("github_id") or ""
    if not isinstance(login, str) or not isinstance(github_id, str):
        raise LaunchplaneSafetyError("invalid_response")
    if login and not GITHUB_LOGIN_RE.fullmatch(login):
        raise LaunchplaneSafetyError("invalid_response")
    if github_id and not GITHUB_ID_RE.fullmatch(github_id):
        raise LaunchplaneSafetyError("invalid_response")
    return {"github_login": login, "github_id": github_id}


def _project_product_owner_plan(result: object) -> dict[str, object]:
    source = _require_dict(result)
    if any(str(key) not in PRODUCT_OWNER_PLAN_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    operation = source.get("operation")
    if operation not in PRODUCT_OWNER_OPERATIONS:
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "status": public_code(source.get("status")),
        "mode": _reviewed_plan_mode(source.get("mode")),
        "product": public_identifier(source.get("product")),
        "operation": operation,
        "resolved_owner": _project_owner_identity(
            {
                "github_login": source.get("resolved_github_login"),
                "github_id": source.get("resolved_github_id"),
            }
        ),
        "owner_before": _project_owner_identity(source.get("owner_before")),
        "owner_after": _project_owner_identity(source.get("owner_after")),
        "changed": bool(_optional_bool(source.get("changed"))),
        "applied": bool(_optional_bool(source.get("applied"))),
        "reason": public_summary_string(source.get("reason")),
        "source_label": public_identifier(source.get("source_label")),
    }
    for field in ("profile_updated_at_before", "profile_updated_at_after"):
        if source.get(field):
            projected[field] = public_summary_string(source.get(field), max_length=64)
    # Launchplane does not bind an owner apply to a dry-run; this digest is the helper's.
    projected["plan_sha256"] = _canonical_sha256(
        {
            field: projected[field]
            for field in ("product", "operation", "owner_before", "owner_after", "reason")
        }
    )
    assert_public_safe_shape(projected)
    return projected


def _project_image_repository(value: object, *, pattern: re.Pattern[str]) -> str:
    if value in {None, ""}:
        return ""
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise LaunchplaneSafetyError("invalid_response")
    return public_identifier(value)


def _project_artifact_reference(value: object) -> str:
    artifact_id = _optional_identifier(value)
    if "://" in artifact_id:
        raise LaunchplaneSafetyError("invalid_response")
    return artifact_id


def _project_image_repository_lanes(value: object) -> list[dict[str, object]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > PRODUCT_IMAGE_REPOSITORY_MAX_LANES:
        raise LaunchplaneSafetyError("invalid_response")
    lanes: list[dict[str, object]] = []
    for item in value:
        source = _require_dict(item)
        if any(str(key) not in PRODUCT_IMAGE_REPOSITORY_LANE_FIELDS for key in source):
            raise LaunchplaneSafetyError("unsafe_response_shape")
        lanes.append(
            {
                "instance": public_identifier(source.get("instance")),
                "context": public_identifier(source.get("context")),
                "current_artifact_id": _project_artifact_reference(
                    source.get("current_artifact_id")
                ),
                "in_new_repository": bool(_optional_bool(source.get("in_new_repository"))),
            }
        )
    return lanes


def _image_repository_plan_digest(plan: dict[str, Any]) -> str:
    # Launchplane does not bind an image repository apply to a dry-run; this digest is the
    # helper's. Lane artifacts are shown for review but left out: a deploy may move them.
    return _canonical_sha256(
        {field: plan.get(field) for field in PRODUCT_IMAGE_REPOSITORY_DIGEST_FIELDS}
    )


def _project_product_image_repository_plan(result: object) -> dict[str, object]:
    source = _require_dict(result)
    if any(str(key) not in PRODUCT_IMAGE_REPOSITORY_PLAN_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    projected: dict[str, object] = {
        "status": public_code(source.get("status")),
        "mode": _reviewed_plan_mode(source.get("mode")),
        "product": public_identifier(source.get("product")),
        "repository": public_identifier(source.get("repository")),
        "image_repository_before": _project_image_repository(
            source.get("image_repository_before"), pattern=IMAGE_REPOSITORY_RE
        ),
        "image_repository_after": _project_image_repository(
            source.get("image_repository_after"), pattern=GHCR_IMAGE_REPOSITORY_RE
        ),
        "changed": bool(_optional_bool(source.get("changed"))),
        "applied": bool(_optional_bool(source.get("applied"))),
        "lanes": _project_image_repository_lanes(source.get("lanes")),
        "reason": public_summary_string(source.get("reason")),
        "source_label": public_identifier(source.get("source_label")),
    }
    if not projected["image_repository_after"]:
        raise LaunchplaneSafetyError("invalid_response")
    for field in ("profile_updated_at_before", "profile_updated_at_after"):
        if source.get(field):
            projected[field] = public_summary_string(source.get(field), max_length=64)
    projected["plan_sha256"] = _image_repository_plan_digest(projected)
    assert_public_safe_shape(projected)
    return projected


def _project_backup_policy_summary(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    source = _require_dict(value)
    if any(str(key) not in PRODUCTION_BACKUP_POLICY_SUMMARY_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    if source.get("status") not in PRODUCTION_BACKUP_RECORD_STATUSES:
        raise LaunchplaneSafetyError("invalid_response")
    return {
        "policy_id": public_identifier(source.get("policy_id")),
        "record_id": public_identifier(source.get("record_id")),
        "policy_revision": _positive_int(source.get("policy_revision")),
        "status": source["status"],
        "promotion_action": public_identifier(source.get("promotion_action")),
        "source_target_id": public_identifier(source.get("source_target_id")),
        "destination_target_id": public_identifier(source.get("destination_target_id")),
        "effective_at": public_summary_string(source.get("effective_at"), max_length=64),
        "review_after": public_summary_string(source.get("review_after"), max_length=64),
    }


def _project_backup_target_summaries(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > PRODUCTION_BACKUP_MAX_TARGETS:
        raise LaunchplaneSafetyError("invalid_response")
    projected: list[dict[str, object]] = []
    for item in value:
        source = _require_dict(item)
        if any(str(key) not in PRODUCTION_BACKUP_TARGET_SUMMARY_FIELDS for key in source):
            raise LaunchplaneSafetyError("unsafe_response_shape")
        if (
            source.get("status") not in PRODUCTION_BACKUP_RECORD_STATUSES
            or source.get("provider_type") != "proxmox"
            or source.get("destination_kind") not in PRODUCTION_BACKUP_DESTINATION_KINDS
        ):
            raise LaunchplaneSafetyError("invalid_response")
        projected.append(
            {
                "target_id": public_identifier(source.get("target_id")),
                "record_id": public_identifier(source.get("record_id")),
                "target_revision": _positive_int(source.get("target_revision")),
                "status": source["status"],
                "provider_type": "proxmox",
                "destination_kind": source["destination_kind"],
                "effective_at": public_summary_string(source.get("effective_at"), max_length=64),
                "review_after": public_summary_string(source.get("review_after"), max_length=64),
            }
        )
    return projected


def _project_production_backup_authority_result(result: object) -> dict[str, object]:
    source = _require_dict(result)
    if any(str(key) not in PRODUCTION_BACKUP_AUTHORITY_RESULT_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    if (
        source.get("mode") not in PRODUCTION_BACKUP_AUTHORITY_MODES
        or source.get("status") not in PRODUCTION_BACKUP_AUTHORITY_STATUSES
    ):
        raise LaunchplaneSafetyError("invalid_response")
    policy = _project_backup_policy_summary(source.get("policy"))
    if policy is None:
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "schema_version": _positive_int(source.get("schema_version", 1)),
        "mode": source["mode"],
        "status": source["status"],
        "authority_digest": _project_sha256(source.get("authority_digest")),
        "policy": policy,
        "targets": _project_backup_target_summaries(source.get("targets", [])),
    }
    assert_public_safe_shape(projected)
    return projected


def _project_production_backup_authority_read(value: object) -> dict[str, object]:
    source = _require_dict(value)
    if any(str(key) not in PRODUCTION_BACKUP_AUTHORITY_READ_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    if source.get("state") not in PRODUCTION_BACKUP_AUTHORITY_STATES:
        raise LaunchplaneSafetyError("invalid_response")
    reason_codes = source.get("reason_codes") or []
    if not isinstance(reason_codes, list):
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "product": public_identifier(source.get("product")),
        "context": public_identifier(source.get("context")),
        "instance": public_identifier(source.get("instance")),
        "promotion_action": public_identifier(source.get("promotion_action")),
        "state": source["state"],
        "ready": bool(_optional_bool(source.get("ready"))),
        # The summary is free text that can name hosts; reason codes carry the state.
        # Codes such as production_backup_target_stale:<target id> carry a target id.
        "reason_codes": [public_identifier(code) for code in reason_codes],
        "policy": _project_backup_policy_summary(source.get("policy")),
        "targets": _project_backup_target_summaries(source.get("targets") or []),
        "generated_at": _optional_text(source.get("generated_at")),
    }
    assert_public_safe_shape(projected)
    return projected


def _dokploy_setup_target_id(result: object) -> str:
    """The created compose id, used only to compare with read-back; never printed."""
    setup = _require_dict(_require_dict(result).get("setup"))
    record = setup.get("target_id_record")
    target_id = _require_dict(record).get("target_id") if record is not None else ""
    if not isinstance(target_id, str):
        raise LaunchplaneSafetyError("invalid_response")
    return target_id.strip()


def _validate_compose_source(branch: object, path: object) -> None:
    if (
        not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch)
        or branch.startswith(("-", "/")) or branch.endswith(("/", ".", ".lock"))
        or any(part in branch for part in ("..", "//"))
    ):
        raise ValueError("invalid_compose_source_branch")
    if (
        not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9._/-]+", path)
        or path.startswith("/") or ".." in path.split("/")
        or not path.strip("./")
    ):
        raise ValueError("invalid_compose_source_path")


def _compose_source_digest(source: dict[str, object]) -> str:
    # URL stays private; bind it along with the branch and path to the review.
    return _canonical_sha256({field: source.get(field) for field in (
        "custom_git_url", "custom_git_branch", "compose_path",
    )})


def _project_dokploy_compose_setup(
    result: object, *, request: dict[str, object] | None
) -> dict[str, object]:
    """Plan actions, counts and provider kinds; provider ids, server ids, project and
    environment names, domains, git URLs, env keys and provider requests are dropped."""
    source = _require_dict(result)
    if any(str(key) not in DOKPLOY_COMPOSE_SETUP_RESULT_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    operation = source.get("operation")
    if operation not in {"create-compose", "complete-compose-source"} or operation != (request or {}).get("setup_operation", "create-compose"):
        raise LaunchplaneSafetyError("invalid_response")
    payload_digest = (request or {}).get("payload_digest")
    if not isinstance(payload_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", payload_digest):
        raise LaunchplaneSafetyError("invalid_response")
    setup = _require_dict(source.get("setup"))
    plan = _require_dict(setup.get("plan")) if operation == "create-compose" else {}
    plan_actions = {
        part: public_code(_require_dict(plan.get(part)).get("action"))
        for part in (("project", "environment", "compose") if plan else ())
    }
    target_record = setup.get("target_record")
    target_record = {} if target_record is None else _require_dict(target_record)
    domains = target_record.get("domains") or []
    route_domain_ids = source.get("route_domain_ids") or []
    provider_warnings = setup.get("warnings") or []
    if not all(isinstance(item, list) for item in (domains, route_domain_ids, provider_warnings)):
        raise LaunchplaneSafetyError("invalid_response")
    healthcheck_path = target_record.get("healthcheck_path") or ""
    if not isinstance(healthcheck_path, str) or (
        healthcheck_path and not re.fullmatch(r"/[A-Za-z0-9._~/-]{0,127}", healthcheck_path)
    ):
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "mode": _reviewed_plan_mode(source.get("mode")),
        "operation": operation,
        "context": public_identifier(source.get("context")),
        "instance": public_identifier(source.get("instance")),
        "applied": bool(_optional_bool(source.get("applied"))),
        # The reason comes from the private payload and can name hosts; the digest covers it.
        "plan_actions": plan_actions,
        "healthcheck_path": healthcheck_path,
        "domain_count": len(domains),
        "route_domain_count": len(route_domain_ids),
        "provider_warning_count": len(provider_warnings),
    }
    source_plan = _require_dict(setup.get("source")) if operation == "complete-compose-source" else _require_dict(plan.get("compose"))
    source_inputs = _optional_dict((request or {}).get("source_inputs"))
    if source_inputs is not None and any(
        source_plan.get(field) != source_inputs.get(field)
        for field in ("custom_git_branch", "compose_path")
    ):
        raise LaunchplaneSafetyError("invalid_response")
    if source_plan.get("custom_git_branch"):
        _validate_compose_source(source_plan.get("custom_git_branch"), source_plan.get("compose_path"))
        if not isinstance(source_plan.get("custom_git_url"), str) or not source_plan["custom_git_url"]:
            raise LaunchplaneSafetyError("invalid_response")
        projected["source"] = {
            "custom_git_branch": source_plan["custom_git_branch"],
            "compose_path": source_plan["compose_path"],
            "source_sha256": _compose_source_digest(source_plan),
        }
    elif operation == "complete-compose-source":
        raise LaunchplaneSafetyError("invalid_response")
    if operation == "complete-compose-source":
        target_id = _dokploy_setup_target_id(source)
        if not target_id:
            raise LaunchplaneSafetyError("invalid_response")
        projected["binding_sha256"] = _canonical_sha256(target_id)
    provider_target = setup.get("provider_target_record")
    if provider_target is not None:
        provider_target = _require_dict(provider_target)
        projected["provider_target"] = {
            field: public_code(provider_target.get(field))
            for field in ("provider_id", "target_category", "provider_target_type")
        }
    # The route has no plan digest; bind the private payload to the reviewed plan here.
    projected["plan_sha256"] = _canonical_sha256(
        {
            "payload_digest": payload_digest,
            "context": projected["context"],
            "instance": projected["instance"],
            "plan_actions": plan_actions,
            **({"source": projected["source"]} if "source" in projected else {}),
            **({"binding_sha256": projected["binding_sha256"]} if "binding_sha256" in projected else {}),
        }
    )
    assert_public_safe_shape(projected)
    return projected


def product_owner_body(args: argparse.Namespace, *, mode: str) -> dict[str, object]:
    login = str(getattr(args, "github_login", "") or "").strip().removeprefix("@")
    clear = bool(getattr(args, "clear", False))
    if clear == bool(login):
        raise ValueError("owner_selection_required")
    if login and not GITHUB_LOGIN_RE.fullmatch(login):
        raise ValueError("invalid_github_login")
    return {
        "schema_version": 1,
        "mode": mode,
        "github_login": login,
        "clear": clear,
        "reason": _required_argument(args, "reason"),
    }


def reviewed_product_owner_plan(
    args: argparse.Namespace, body: dict[str, object]
) -> dict[str, Any]:
    """The saved dry-run plan this owner apply must reproduce."""
    expected_plan_digest = _reviewed_apply_digest(args)
    result = _load_reviewed_plan_evidence(
        args, operation="product-owner-dry-run", expected_plan_digest=expected_plan_digest
    )
    resolved = result.get("resolved_owner")
    resolved_login = resolved.get("github_login") if isinstance(resolved, dict) else None
    if (
        result.get("product") != str(args.product).strip()
        # An unchanged plan has nothing to apply.
        or result.get("operation") != ("clear" if body["clear"] else "set")
        or (
            not body["clear"]
            and str(resolved_login or "").lower() != cast(str, body["github_login"]).lower()
        )
        or result.get("reason") != " ".join(cast(str, body["reason"]).split())
        or not isinstance(result.get("owner_before"), dict)
        or not isinstance(result.get("owner_after"), dict)
    ):
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    return result


def product_image_repository_body(args: argparse.Namespace, *, mode: str) -> dict[str, object]:
    image_repository = _required_argument(args, "image_repository").rstrip("/")
    if not GHCR_IMAGE_REPOSITORY_RE.fullmatch(image_repository):
        raise ValueError("invalid_image_repository")
    return {
        "schema_version": 1,
        "mode": mode,
        "image_repository": image_repository,
        "reason": _required_argument(args, "reason"),
    }


def reviewed_product_image_repository_plan(
    args: argparse.Namespace, body: dict[str, object]
) -> dict[str, Any]:
    """The saved dry-run plan this image repository apply must reproduce."""
    expected_plan_digest = _reviewed_apply_digest(args)
    result = _load_reviewed_plan_evidence(
        args,
        operation="product-image-repository-dry-run",
        expected_plan_digest=expected_plan_digest,
    )
    before = result.get("image_repository_before")
    if (
        result.get("product") != str(args.product).strip()
        or result.get("image_repository_after") != body["image_repository"]
        or result.get("reason") != " ".join(cast(str, body["reason"]).split())
        or not isinstance(before, str)
        or not isinstance(result.get("repository"), str)
        # An unchanged plan has nothing to apply.
        or result.get("changed") is not True
        # The saved file must still say what the digest was computed over.
        or _image_repository_plan_digest(result) != expected_plan_digest
    ):
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    return result


def production_backup_authority_payload(args: argparse.Namespace) -> dict[str, object]:
    payload = read_payload_file(args.payload_file)
    if any(key not in PRODUCTION_BACKUP_AUTHORITY_PAYLOAD_FIELDS for key in payload):
        raise ValueError("unsupported_backup_authority_field")
    policy = payload.get("policy")
    if not isinstance(policy, dict):
        raise ValueError("backup_policy_required")
    if not isinstance(payload.get("targets", []), list):
        raise ValueError("invalid_backup_targets")
    for field in ("product", "context", "instance", "promotion_action"):
        value = policy.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"backup_policy_{field}_required")
    return payload


def production_backup_authority_body(
    args: argparse.Namespace, *, mode: str
) -> tuple[dict[str, object], dict[str, object]]:
    """The route body and the public request summary for a private authority payload."""
    payload = production_backup_authority_payload(args)
    policy = cast(dict[str, object], payload["policy"])
    request: dict[str, object] = {
        "mode": mode,
        "payload_source": "private_file",
        "payload_digest": metadata_review_digest(payload),
        "product": public_identifier(policy["product"]),
        "context": public_identifier(policy["context"]),
        "instance": public_identifier(policy["instance"]),
        "promotion_action": public_identifier(policy["promotion_action"]),
    }
    body: dict[str, object] = {**payload, "mode": "apply" if mode == "apply" else "dry_run"}
    if mode == "apply":
        expected_digest = _reviewed_apply_digest(args)
        evidence, _result = _load_reviewed_evidence(
            args,
            operation="production-backup-authority-dry-run",
            expected_digest=expected_digest,
            digest_field="authority_digest",
            evidence_status="ok",
            result_status="would_apply",
            dry_run_mode="dry_run",
        )
        if evidence["request"].get("payload_digest") != request["payload_digest"]:
            raise ValueError("reviewed_dry_run_not_apply_eligible")
        body["reviewed_authority_digest"] = expected_digest
    return body, request


def dokploy_compose_payload(args: argparse.Namespace) -> dict[str, object]:
    payload = read_payload_file(args.payload_file)
    completion = args.command.startswith("dokploy-target-complete-compose-source-")
    allowed = DOKPLOY_COMPOSE_SOURCE_PAYLOAD_FIELDS if completion else DOKPLOY_COMPOSE_PAYLOAD_FIELDS
    if any(key not in allowed for key in payload):
        raise ValueError("unsupported_dokploy_target_field")
    if completion or payload.get("custom_git_branch"):
        _validate_compose_source(payload.get("custom_git_branch"), payload.get("compose_path"))
    if completion:
        if payload.get("instance") != "testing":
            raise ValueError("compose_source_requires_testing")
        for field in ("context", "reason"):
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field}_required")
        return payload
    for field in ("context", "instance", "target_name", "server_id", "reason"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field}_required")
    if not any(payload.get(field) for field in ("project_id", "project_name", "environment_id")):
        raise ValueError("dokploy_project_required")
    healthcheck_path = payload.get("healthcheck_path")
    # Stable-lane repair derives the lane's health URL from this path.
    if not isinstance(healthcheck_path, str) or not healthcheck_path.startswith("/"):
        raise ValueError("healthcheck_path_required")
    domains = payload.get("domains")
    # Stable-lane repair refuses a target without the lane's domain.
    if (
        not isinstance(domains, list)
        or not domains
        or not all(isinstance(domain, str) and domain.strip() for domain in domains)
    ):
        raise ValueError("domains_required")
    return payload


def dokploy_compose_body(
    args: argparse.Namespace, *, mode: str
) -> tuple[dict[str, object], dict[str, object]]:
    payload = dokploy_compose_payload(args)
    setup_operation = "complete-compose-source" if args.command.startswith("dokploy-target-complete-compose-source-") else "create-compose"
    request: dict[str, object] = {
        "mode": mode,
        "payload_source": "private_file",
        "payload_digest": metadata_review_digest(payload),
        "context": public_identifier(payload["context"]),
        "instance": public_identifier(payload["instance"]),
        "setup_operation": setup_operation,
    }
    if payload.get("custom_git_branch"):
        request["source_inputs"] = {field: payload[field] for field in ("custom_git_branch", "compose_path")}
    body: dict[str, object] = {
        **payload,
        "operation": setup_operation,
        # Target setup is authorized on Launchplane's own service product.
        "product": "launchplane",
        "mode": mode,
    }
    if mode == "apply":
        expected_plan_digest = _reviewed_apply_digest(args)
        evidence, _result = _load_reviewed_evidence(
            args,
            operation=args.command.removesuffix("-apply") + "-dry-run",
            expected_digest=expected_plan_digest,
            result_status=None,
        )
        if evidence["request"].get("payload_digest") != request["payload_digest"]:
            raise ValueError("reviewed_dry_run_not_apply_eligible")
        body["confirmation"] = DOKPLOY_TARGET_SETUP_CONFIRMATION
    return body, request


def read_product_owner(
    *, settings: dict[str, str], product: str, timeout: float
) -> dict[str, str]:
    provider_payload = request_launchplane_read(
        service_url=settings["service_url"],
        path=_product_read_path("product-profile-read", product=product),
        settings=settings,
        query={},
        timeout=timeout,
    )
    profile = _require_dict(provider_payload.get("profile"))
    return _project_owner_identity(profile.get("owner"))


def read_product_image_repository(
    *, settings: dict[str, str], product: str, timeout: float
) -> str:
    provider_payload = request_launchplane_read(
        service_url=settings["service_url"],
        path=_product_read_path("product-profile-read", product=product),
        settings=settings,
        query={},
        timeout=timeout,
    )
    profile = _require_dict(provider_payload.get("profile"))
    image = _optional_dict(profile.get("image")) or {}
    repository = image.get("repository") or ""
    if not isinstance(repository, str):
        raise LaunchplaneSafetyError("invalid_response")
    return _project_image_repository(
        repository.strip().rstrip("/"), pattern=IMAGE_REPOSITORY_RE
    )


def read_production_backup_authority(
    *, settings: dict[str, str], query: dict[str, str], timeout: float
) -> dict[str, object]:
    provider_payload = request_launchplane_read(
        service_url=settings["service_url"],
        path=helper_command_path("production-backup-authority-read"),
        settings=settings,
        query=query,
        timeout=timeout,
    )
    if any(str(key) not in {"status", "trace_id", "authority"} for key in provider_payload):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    return _project_production_backup_authority_read(provider_payload.get("authority"))


def _domain_set(value: object) -> frozenset[str]:
    if not isinstance(value, list):
        return frozenset()
    return frozenset(str(domain).strip().lower() for domain in value)


def read_dokploy_target(
    *, settings: dict[str, str], context: str, instance: str, timeout: float
) -> tuple[dict[str, object], dict[str, object]]:
    """Public target state, plus the ids, domains and health path the records hold,
    for comparison only."""
    provider_payload = request_launchplane_read(
        service_url=settings["service_url"],
        path=internal_helper_path("dokploy-target-inspect"),
        settings=settings,
        query={"context": context, "instance": instance},
        timeout=timeout,
    )
    inspect = _require_dict(provider_payload.get("inspect"))
    tracked = _require_dict(inspect.get("tracked_target") or {})
    provider_target = _require_dict(inspect.get("provider_target_record") or {})
    live_provider = _require_dict(inspect.get("provider") or {})
    private = {
        "target_ids": {
            str(source.get("target_id") or "").strip()
            for source in (inspect, tracked, provider_target)
        },
        "domains": _domain_set(tracked.get("domains")),
        "healthcheck_path": str(tracked.get("healthcheck_path") or ""),
        "tracked_source": {field: tracked.get(field) for field in (
            "source_type", "custom_git_url", "custom_git_branch", "compose_path",
        )},
        "live_source": {field: live_provider.get(field) for field in (
            "source_type", "custom_git_url", "custom_git_branch", "compose_path",
        )},
    }
    public = {
        "status": public_code(inspect.get("status")),
        "target_type": public_code(inspect.get("target_type"), default="unknown"),
        "provider_target_record": public_code(provider_target.get("status"), default="missing"),
    }
    return public, private


def attach_read_back(
    payload: dict[str, Any], *, read: Any, matches: Any, label: str
) -> bool:
    """Read the record back after an accepted apply; a failed read is never a success."""
    try:
        observed = read()
    except (OSError, TimeoutError, ValueError, LaunchplaneSafetyError):
        payload["warnings"].append(
            warning(
                "read_back_unavailable",
                f"The apply was accepted, but the {label} could not be read back. "
                "Read it again before relying on it or retrying.",
            )
        )
        return False
    payload["result"]["read_back"] = observed
    payload["result"]["read_back_matches"] = bool(matches(observed))
    if not payload["result"]["read_back_matches"]:
        payload["warnings"].append(
            warning(
                "read_back_mismatch",
                f"The {label} read back does not match the reviewed plan. Do not retry; "
                "inspect the record and its trace.",
            )
        )
    return payload["result"]["read_back_matches"]


def execute_verified_apply(
    *,
    args: argparse.Namespace,
    operation: str,
    request: dict[str, object],
    path: str,
    body: dict[str, object],
    preflight: Any,
    finish: Any,
    label: str,
) -> int:
    """POST a reviewed apply after `preflight` finds the record unchanged, then let
    `finish` compare the result and a read-back with the review. Exit 0 only when both match."""
    settings = prepare_operator_settings(args=args, operation=operation, request=request)
    if settings is None:
        return 2
    post_attempted = False
    try:
        stale_summary = preflight(settings)
        if stale_summary is not None:
            payload = base_payload(status="stale", operation=operation, request=request)
            payload["summary"] = stale_summary
            assert_public_safe_shape(payload["summary"])
            emit(payload)
            return 1
        post_attempted = True
        provider_payload = request_launchplane(
            service_url=settings["service_url"],
            path=path,
            settings=settings,
            body=body,
            timeout=args.timeout,
            idempotency_key=args.idempotency_key,
        )
        payload = summarize_success(
            operation=operation, request=request, provider_payload=provider_payload
        )
        verified = finish(settings, provider_payload, payload)
        if not verified:
            payload["status"] = "accepted_unverified"
            cast(dict[str, object], payload["summary"])["recommendation"] = (
                f"Launchplane accepted the apply, but it could not be verified against the "
                f"reviewed plan. Read back the {label} before any retry."
            )
        for part in ("result", "summary", "warnings"):
            assert_public_safe_shape(payload[part])
        emit(payload)
        return 0 if verified else 1
    except urllib.error.HTTPError as exc:
        # A gateway error after the POST began may follow a write Launchplane completed.
        if post_attempted and (exc.code >= 500 or exc.code == 408):
            unknown = _apply_outcome_unknown(operation=operation, request=request, label=label)
            if operation.startswith("dokploy-target-"):
                try:
                    error = summarize_http_error(operation=operation, request=request, exc=exc)
                    unknown_summary = _require_dict(unknown["summary"])
                    error_summary = _require_dict(error["summary"])
                    unknown_summary.update({field: error_summary[field] for field in (
                        "http_status", "trace_id", "error_code",
                    )})
                    if error_summary["error_code"] == "dokploy_source_partial_outcome" or request.get("source_inputs"):
                        unknown_summary["recommendation"] = (
                            "Source setup may have partially changed the provider. Require admin "
                            "reconciliation before any retry under any key; never replace or adopt the target."
                        )
                except LaunchplaneSafetyError:
                    pass
            emit(unknown)
            return 1
        emit_http_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except LaunchplaneSafetyError as exc:
        if post_attempted and exc.code == "unsafe_redirect":
            emit(_apply_outcome_unknown(operation=operation, request=request, label=label))
            return 1
        if post_attempted:
            emit(_apply_response_unverified(operation=operation, request=request, label=label))
            return 1
        emit_safety_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError):
        if post_attempted:
            emit(_apply_outcome_unknown(operation=operation, request=request, label=label))
            return 1
        emit_provider_unavailable(operation=operation, request=request)
        return 1
    except (ValueError, json.JSONDecodeError):
        if post_attempted:
            emit(_apply_response_unverified(operation=operation, request=request, label=label))
            return 1
        emit_invalid_response(operation=operation, request=request)
        return 1


def _apply_response_unverified(
    *, operation: str, request: dict[str, object], label: str
) -> dict[str, object]:
    payload = base_payload(status="accepted_unverified", operation=operation, request=request)
    payload["summary"] = {
        "recommendation": (
            f"Launchplane answered the apply with a response that could not be verified "
            f"locally. Read back the {label} before any retry."
        )
    }
    payload["warnings"] = [
        warning(
            "apply_response_unverified",
            "The apply response was not safe to project; do not retry before read-back.",
        )
    ]
    return payload


def _apply_outcome_unknown(
    *, operation: str, request: dict[str, object], label: str
) -> dict[str, object]:
    payload = base_payload(status="outcome_unknown", operation=operation, request=request)
    payload["summary"] = {
        "recommendation": (
            f"The apply outcome is unknown because the exchange failed after the POST began. "
            f"Read back the {label} before any retry."
        )
    }
    payload["warnings"] = [
        warning("apply_outcome_unknown", "Do not retry this apply until read-back resolves it.")
    ]
    return payload


def execute_product_owner_apply(
    *, args: argparse.Namespace, request: dict[str, object], body: dict[str, object]
) -> int:
    reviewed = reviewed_product_owner_plan(args, body)
    product = str(args.product).strip()
    path = _product_read_path("product-owner-apply", product=product)

    def preflight(settings: dict[str, str]) -> dict[str, object] | None:
        current = read_product_owner(settings=settings, product=product, timeout=args.timeout)
        if current == reviewed["owner_before"]:
            return None
        return {
            "error_code": "owner_changed_since_review",
            "recommendation": "Stop before apply and dry-run again against the current Client.",
        }

    def finish(
        settings: dict[str, str], _provider_payload: dict[str, Any], payload: dict[str, Any]
    ) -> bool:
        result = payload["result"]
        applied_as_reviewed = result.get("applied") is True and all(
            result.get(field) == reviewed[field]
            for field in ("operation", "owner_before", "owner_after")
        )
        if not applied_as_reviewed:
            payload["warnings"].append(
                warning(
                    "applied_plan_differs_from_review",
                    "Launchplane applied a different Client change than the reviewed dry-run.",
                )
            )
        read_back_ok = attach_read_back(
            payload,
            read=lambda: read_product_owner(
                settings=settings, product=product, timeout=args.timeout
            ),
            matches=lambda observed: observed == reviewed["owner_after"],
            label="product Client",
        )
        return applied_as_reviewed and read_back_ok

    return execute_verified_apply(
        args=args,
        operation="product-owner-apply",
        request=request,
        path=path,
        body=body,
        preflight=preflight,
        finish=finish,
        label="product Client",
    )


def execute_product_image_repository_apply(
    *, args: argparse.Namespace, request: dict[str, object], body: dict[str, object]
) -> int:
    reviewed = reviewed_product_image_repository_plan(args, body)
    product = str(args.product).strip()
    path = _product_read_path("product-image-repository-apply", product=product)
    # Launchplane refuses the apply with `stale` when this is no longer the profile's repository.
    body["expected_image_repository"] = reviewed["image_repository_before"]

    def preflight(settings: dict[str, str]) -> dict[str, object] | None:
        current = read_product_image_repository(
            settings=settings, product=product, timeout=args.timeout
        )
        if current == reviewed["image_repository_before"]:
            return None
        return {
            "error_code": "image_repository_changed_since_review",
            "recommendation": "Stop before apply and dry-run again against the current profile.",
        }

    def finish(
        settings: dict[str, str], _provider_payload: dict[str, Any], payload: dict[str, Any]
    ) -> bool:
        result = payload["result"]
        applied_as_reviewed = result.get("applied") is True and all(
            result.get(field) == reviewed[field]
            for field in ("repository", "image_repository_before", "image_repository_after")
        )
        if not applied_as_reviewed:
            payload["warnings"].append(
                warning(
                    "applied_plan_differs_from_review",
                    "Launchplane applied a different image repository change than the "
                    "reviewed dry-run.",
                )
            )
        read_back_ok = attach_read_back(
            payload,
            read=lambda: read_product_image_repository(
                settings=settings, product=product, timeout=args.timeout
            ),
            matches=lambda observed: observed == reviewed["image_repository_after"],
            label="product image repository",
        )
        return applied_as_reviewed and read_back_ok

    return execute_verified_apply(
        args=args,
        operation="product-image-repository-apply",
        request=request,
        path=path,
        body=body,
        preflight=preflight,
        finish=finish,
        label="product image repository",
    )


def execute_production_backup_authority_apply(
    *, args: argparse.Namespace, request: dict[str, object], body: dict[str, object]
) -> int:
    policy = cast(dict[str, Any], body["policy"])
    query = {
        "product": str(policy["product"]).strip().lower(),
        "context": str(policy["context"]).strip().lower(),
        "instance": str(policy["instance"]).strip().lower(),
        "promotion_action": str(policy["promotion_action"]).strip(),
    }
    expected_digest = cast(str, body["reviewed_authority_digest"])
    _evidence, reviewed = _load_reviewed_evidence(
        args,
        operation="production-backup-authority-dry-run",
        expected_digest=expected_digest,
        digest_field="authority_digest",
        evidence_status="ok",
        result_status="would_apply",
        dry_run_mode="dry_run",
    )
    reviewed_policy = reviewed.get("policy") if isinstance(reviewed.get("policy"), dict) else {}
    # The saved evidence holds projected strings; anything else simply fails to match.
    reviewed_targets = {
        cast(str, target.get("target_id")): cast(str, target.get("record_id"))
        for target in reviewed.get("targets") or []
        if isinstance(target, dict)
    }

    def finish(
        settings: dict[str, str], _provider_payload: dict[str, Any], payload: dict[str, Any]
    ) -> bool:
        result = payload["result"]
        # Launchplane enforces the reviewed digest; this confirms what it reports.
        applied_targets = {
            str(target["target_id"]): str(target["record_id"])
            for target in cast(list[dict[str, Any]], result["targets"])
        }
        # Compare with the saved review, so a target the response omits is not skipped.
        applied_as_reviewed = (
            result.get("authority_digest") == expected_digest
            and result.get("status") in {"applied", "replayed"}
            and cast(dict[str, Any], result["policy"]).get("record_id")
            == reviewed_policy.get("record_id")
            and applied_targets == reviewed_targets
        )
        if not applied_as_reviewed:
            payload["warnings"].append(
                warning(
                    "applied_plan_differs_from_review",
                    "Launchplane reported a different backup authority than the reviewed dry-run.",
                )
            )
        def matches(observed: dict[str, Any]) -> bool:
            policy_read = observed.get("policy") or {}
            read_targets = {
                str(target["target_id"]): str(target["record_id"])
                for target in observed.get("targets") or []
            }
            # A reviewed target the read-back lacks is a mismatch, not a skip.
            return policy_read.get("record_id") == reviewed_policy.get("record_id") and all(
                read_targets.get(target_id) == record_id
                for target_id, record_id in reviewed_targets.items()
            )

        read_back_ok = attach_read_back(
            payload,
            read=lambda: read_production_backup_authority(
                settings=settings, query=query, timeout=args.timeout
            ),
            matches=matches,
            label="production backup authority",
        )
        return applied_as_reviewed and read_back_ok

    return execute_verified_apply(
        args=args,
        operation="production-backup-authority-apply",
        request=request,
        path=helper_command_path("production-backup-authority-apply"),
        body=body,
        preflight=lambda _settings: None,
        finish=finish,
        label="production backup authority",
    )


def execute_dokploy_compose_apply(
    *, args: argparse.Namespace, request: dict[str, object], body: dict[str, object]
) -> int:
    context = cast(str, body["context"]).strip()
    instance = cast(str, body["instance"]).strip()
    expected_plan_digest = args.expected_plan_digest.strip().lower()
    completion = body["operation"] == "complete-compose-source"
    _evidence, reviewed = _load_reviewed_evidence(
        args, operation=args.command.removesuffix("-apply") + "-dry-run",
        expected_digest=expected_plan_digest, result_status=None,
    )

    def preflight(settings: dict[str, str]) -> dict[str, object] | None:
        if completion:
            _public, private = read_dokploy_target(
                settings=settings, context=context, instance=instance, timeout=args.timeout
            )
            target_ids = cast(set[str], private["target_ids"])
            if target_ids != {""} and len(target_ids) == 1:
                (target_id,) = target_ids
                if _canonical_sha256(target_id) == reviewed.get("binding_sha256"):
                    return None
            return {
                "error_code": "compose_binding_changed_since_review",
                "recommendation": "The tracked binding changed since review; run a new dry-run.",
            }
        return None

    def finish(
        settings: dict[str, str], provider_payload: dict[str, Any], payload: dict[str, Any]
    ) -> bool:
        result = payload["result"]
        created_target_id = _dokploy_setup_target_id(provider_payload.get("result"))
        applied_as_reviewed = (
            result.get("applied") is True
            and result.get("plan_sha256") == expected_plan_digest
            and bool(created_target_id)
        )
        if not applied_as_reviewed:
            payload["warnings"].append(
                warning(
                    "applied_plan_differs_from_review",
                    "Launchplane did not report a created target for the reviewed plan.",
                )
            )
        result["reviewed_plan_sha256"] = expected_plan_digest

        def read() -> dict[str, object]:
            public, private = read_dokploy_target(
                settings=settings, context=context, instance=instance, timeout=args.timeout
            )
            # Compared here and never printed: the records name the created compose and
            # hold the reviewed domains and health path.
            public["target_ids_agree"] = private["target_ids"] == {created_target_id}
            public["configuration_matches_review"] = completion or (
                private["domains"] == _domain_set(body["domains"])
                and private["healthcheck_path"] == body["healthcheck_path"]
            )
            expected_source = result.get("source")
            public["source_matches_review"] = expected_source is None or all(
                observed_source.get("source_type") == "git"
                and _compose_source_digest(observed_source) == expected_source["source_sha256"]
                for observed_source in (
                    cast(dict[str, object], private["tracked_source"]),
                    cast(dict[str, object], private["live_source"]),
                )
            )
            return public

        read_back_ok = attach_read_back(
            payload,
            read=read,
            matches=lambda observed: observed["provider_target_record"] == "present"
            and observed["target_ids_agree"] is True
            and observed["configuration_matches_review"] is True
            and observed["source_matches_review"] is True,
            label="Dokploy target",
        )
        return applied_as_reviewed and read_back_ok

    return execute_verified_apply(
        args=args,
        operation=args.command,
        request=request,
        path=helper_command_path(args.command),
        body=body,
        # Completion compares the saved binding before any provider write.
        preflight=preflight,
        finish=finish,
        label="Dokploy target",
    )


def execute_production_backup_authority_read(
    *, args: argparse.Namespace, request: dict[str, object]
) -> int:
    operation = "production-backup-authority-read"
    settings = prepare_operator_settings(args=args, operation=operation, request=request)
    if settings is None:
        return 2
    try:
        provider_payload = request_launchplane_read(
            service_url=settings["service_url"],
            path=helper_command_path(operation),
            settings=settings,
            query={
                "product": args.product,
                "context": args.context,
                "instance": args.instance,
                "promotion_action": args.promotion_action,
            },
            timeout=args.timeout,
        )
        if any(str(key) not in {"status", "trace_id", "authority"} for key in provider_payload):
            raise LaunchplaneSafetyError("unsafe_response_shape")
        status = public_code(provider_payload.get("status"), default="ok")
        payload = base_payload(status=status, operation=operation, request=request)
        payload["result"] = _project_production_backup_authority_read(
            provider_payload.get("authority")
        )
        payload["summary"] = {
            "launchplane_status": status,
            "trace_id": public_trace_id(provider_payload.get("trace_id")),
            "recommendation": (
                "Use policy.record_id and each target's record_id as the expected current "
                "record ids in the next authority payload."
            ),
        }
        assert_public_safe_shape(payload["summary"])
        emit(payload)
        return 0
    except urllib.error.HTTPError as exc:
        emit_http_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except LaunchplaneSafetyError as exc:
        emit_safety_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError):
        emit_provider_unavailable(operation=operation, request=request)
        return 1
    except (ValueError, json.JSONDecodeError):
        emit_invalid_response(operation=operation, request=request)
        return 1


# Private health endpoints: the record a `private_http` health check names. The
# URL is private by definition, so it is sent from the private payload, compared
# with what Launchplane holds, and never printed. Launchplane does not bind an
# apply to its dry-run; the helper digests the reviewed payload and plan instead.

PRIVATE_HEALTH_ENDPOINT_PAYLOAD_FIELDS = {
    "schema_version",
    "endpoint_key",
    "product",
    "context",
    "instance",
    "url",
    "status",
    "source_label",
}
PRIVATE_HEALTH_ENDPOINT_RECORD_FIELDS = PRIVATE_HEALTH_ENDPOINT_PAYLOAD_FIELDS | {"updated_at"}
PRIVATE_HEALTH_ENDPOINT_RESULT_FIELDS = {"mode", "endpoint_key", "endpoint_status", "record"}
PRIVATE_HEALTH_ENDPOINT_LIST_FIELDS = {
    "status",
    "trace_id",
    "product",
    "context",
    "instance",
    "limit",
    "count",
    "records",
}
PRIVATE_HEALTH_ENDPOINT_STATUSES = {"active", "disabled"}
PRIVATE_HEALTH_ENDPOINT_PLAN_STATUSES = {"dry-run": "planned", "apply": "applied"}
PRIVATE_HEALTH_ENDPOINT_MAX_RECORDS = 100
# Launchplane's typed confirmation for a private health endpoint apply; the reviewed
# dry-run evidence and plan digest are what the helper checks before sending it.
PRIVATE_HEALTH_ENDPOINT_CONFIRMATION = "APPLY LAUNCHPLANE PRIVATE HEALTH ENDPOINT"


def _project_private_health_endpoint_record(value: object) -> dict[str, object]:
    """Key, scope, status and update time; the URL and free-text source label are dropped."""
    source = _require_dict(value)
    if any(str(key) not in PRIVATE_HEALTH_ENDPOINT_RECORD_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    if source.get("status") not in PRIVATE_HEALTH_ENDPOINT_STATUSES:
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "endpoint_key": public_identifier(source.get("endpoint_key")),
        "product": public_identifier(source.get("product")),
        "context": public_identifier(source.get("context")),
        "instance": public_identifier(source.get("instance")),
        "status": source["status"],
        "updated_at": _optional_text(source.get("updated_at")),
    }
    assert_public_safe_shape(projected)
    return projected


def _private_health_endpoint_url(value: object) -> str:
    """The record's URL, used only to compare with the reviewed payload; never printed."""
    url = _require_dict(value).get("url")
    if not isinstance(url, str):
        raise LaunchplaneSafetyError("invalid_response")
    return url.strip()


PRIVATE_HEALTH_ENDPOINT_PLAN_FIELDS = ("endpoint_key", "product", "context", "instance", "status")


def _private_health_endpoint_plan_sha256(payload_digest: str, record: dict[str, Any]) -> str:
    """The route has no plan digest; bind the private payload to the planned record here."""
    return _canonical_sha256(
        {
            "payload_digest": payload_digest,
            **{field: record.get(field) for field in PRIVATE_HEALTH_ENDPOINT_PLAN_FIELDS},
        }
    )


def _project_private_health_endpoint_plan(
    result: object, *, request: dict[str, object] | None
) -> dict[str, object]:
    source = _require_dict(result)
    if any(str(key) not in PRIVATE_HEALTH_ENDPOINT_RESULT_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    mode = _reviewed_plan_mode(source.get("mode"))
    if source.get("endpoint_status") != PRIVATE_HEALTH_ENDPOINT_PLAN_STATUSES[mode]:
        raise LaunchplaneSafetyError("invalid_response")
    payload_digest = (request or {}).get("payload_digest")
    if not isinstance(payload_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", payload_digest):
        raise LaunchplaneSafetyError("invalid_response")
    record = _project_private_health_endpoint_record(source.get("record"))
    if public_identifier(source.get("endpoint_key")) != record["endpoint_key"]:
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "mode": mode,
        "endpoint_key": record["endpoint_key"],
        "endpoint_status": source["endpoint_status"],
        "record": record,
        "plan_sha256": _private_health_endpoint_plan_sha256(payload_digest, record),
    }
    assert_public_safe_shape(projected)
    return projected


def private_health_endpoint_payload(args: argparse.Namespace) -> dict[str, object]:
    payload = read_payload_file(args.payload_file)
    if any(key not in PRIVATE_HEALTH_ENDPOINT_PAYLOAD_FIELDS for key in payload):
        raise ValueError("unsupported_private_health_endpoint_field")
    if payload.get("schema_version", 1) != 1:
        raise ValueError("unsupported_private_health_endpoint_schema_version")
    for field in ("endpoint_key", "product", "context", "instance"):
        value = payload.get(field)
        # The key is a path segment on read-back; the scope is echoed in the output.
        if not isinstance(value, str) or not PRODUCT_READ_PATH_SEGMENT_RE.fullmatch(value.strip()):
            raise ValueError(f"{field}_required")
    url = payload.get("url")
    # Launchplane refuses a public URL; the helper only checks it is an HTTP URL at all.
    if not isinstance(url, str) or urllib.parse.urlsplit(url.strip()).scheme not in {
        "http",
        "https",
    }:
        raise ValueError("private_url_required")
    if payload.get("status", "active") not in PRIVATE_HEALTH_ENDPOINT_STATUSES:
        raise ValueError("invalid_private_health_endpoint_status")
    if not isinstance(payload.get("source_label", ""), str):
        raise ValueError("invalid_source_label")
    return payload


def private_health_endpoint_body(
    args: argparse.Namespace, *, mode: str
) -> tuple[dict[str, object], dict[str, object]]:
    """The route body and the public request summary for a private endpoint payload."""
    payload = private_health_endpoint_payload(args)
    reason = _required_argument(args, "reason")
    # Sorted, so a retry sends the same body however the payload file orders its keys.
    endpoint = {
        field: value.strip() if isinstance(value, str) else value
        for field, value in sorted(payload.items())
    }
    endpoint.setdefault("status", "active")
    request: dict[str, object] = {
        "mode": mode,
        "payload_source": "private_file",
        "payload_digest": metadata_review_digest({**endpoint, "reason": reason}),
        "endpoint_key": public_identifier(endpoint["endpoint_key"]),
        "product": public_identifier(endpoint["product"]),
        "context": public_identifier(endpoint["context"]),
        "instance": public_identifier(endpoint["instance"]),
        "endpoint_status": endpoint["status"],
    }
    endpoint["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    body: dict[str, object] = {
        "schema_version": 1,
        "mode": mode,
        "endpoint": endpoint,
        "reason": reason,
    }
    if mode == "apply":
        expected_plan_digest = _reviewed_apply_digest(args)
        evidence, result = _load_reviewed_evidence(
            args,
            operation="private-health-endpoint-dry-run",
            expected_digest=expected_plan_digest,
            result_status=None,
        )
        reviewed_record = result.get("record")
        if not isinstance(reviewed_record, dict):
            raise ValueError("reviewed_dry_run_not_apply_eligible")
        reviewed_updated_at = reviewed_record.get("updated_at")
        # Recompute the reviewed digest from this payload, so neither an edited payload
        # nor edited evidence can send a record the reviewer did not see.
        if (
            evidence["request"].get("payload_digest") != request["payload_digest"]
            or _private_health_endpoint_plan_sha256(
                cast(str, request["payload_digest"]), reviewed_record
            )
            != expected_plan_digest
            or any(
                reviewed_record.get(field) != endpoint[field]
                for field in PRIVATE_HEALTH_ENDPOINT_PLAN_FIELDS
            )
            or not isinstance(reviewed_updated_at, str)
            or not reviewed_updated_at
        ):
            raise ValueError("reviewed_dry_run_not_apply_eligible")
        # Launchplane fingerprints the whole body for idempotency, so a retry of this
        # reviewed apply must send the same timestamp: the one the reviewer saw.
        endpoint["updated_at"] = reviewed_updated_at
        body["confirmation"] = PRIVATE_HEALTH_ENDPOINT_CONFIRMATION
    return body, request


def read_private_health_endpoint(
    *, settings: dict[str, str], endpoint: dict[str, Any], timeout: float
) -> tuple[dict[str, object], str]:
    """The public record, plus its URL for comparison only."""
    path = internal_helper_path("private-health-endpoint-record-read").format(
        endpoint_key=urllib.parse.quote(cast(str, endpoint["endpoint_key"]), safe="")
    )
    provider_payload = request_launchplane_read(
        service_url=settings["service_url"],
        path=path,
        settings=settings,
        query={field: cast(str, endpoint[field]) for field in ("product", "context", "instance")},
        timeout=timeout,
    )
    if any(str(key) not in {"status", "trace_id", "record"} for key in provider_payload):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    record = provider_payload.get("record")
    return _project_private_health_endpoint_record(record), _private_health_endpoint_url(record)


def execute_private_health_endpoint_apply(
    *, args: argparse.Namespace, request: dict[str, object], body: dict[str, object]
) -> int:
    endpoint = cast(dict[str, Any], body["endpoint"])
    expected_plan_digest = args.expected_plan_digest.strip().lower()

    def finish(
        settings: dict[str, str], provider_payload: dict[str, Any], payload: dict[str, Any]
    ) -> bool:
        result = payload["result"]
        applied_url = _private_health_endpoint_url(
            _require_dict(provider_payload.get("result")).get("record")
        )
        applied_as_reviewed = (
            result.get("endpoint_status") == "applied"
            and result.get("plan_sha256") == expected_plan_digest
            and applied_url == endpoint["url"]
        )
        if not applied_as_reviewed:
            payload["warnings"].append(
                warning(
                    "applied_plan_differs_from_review",
                    "Launchplane reported a different endpoint record than the reviewed dry-run.",
                )
            )
        result["reviewed_plan_sha256"] = expected_plan_digest

        def read() -> dict[str, object]:
            public, url = read_private_health_endpoint(
                settings=settings, endpoint=endpoint, timeout=args.timeout
            )
            # Compared here and never printed.
            public["url_matches_review"] = url == endpoint["url"]
            return public

        read_back_ok = attach_read_back(
            payload,
            read=read,
            matches=lambda observed: observed["url_matches_review"] is True
            and all(
                observed[field] == endpoint[field] for field in PRIVATE_HEALTH_ENDPOINT_PLAN_FIELDS
            ),
            label="private health endpoint",
        )
        return applied_as_reviewed and read_back_ok

    return execute_verified_apply(
        args=args,
        operation="private-health-endpoint-apply",
        request=request,
        path=helper_command_path("private-health-endpoint-apply"),
        body=body,
        # Launchplane refuses a key that belongs to another product, context or instance.
        preflight=lambda _settings: None,
        finish=finish,
        label="private health endpoint",
    )


def execute_private_health_endpoint_read(
    *, args: argparse.Namespace, request: dict[str, object]
) -> int:
    operation = "private-health-endpoint-read"
    settings = prepare_operator_settings(args=args, operation=operation, request=request)
    if settings is None:
        return 2
    query = {"product": args.product, "context": args.context}
    if args.instance:
        query["instance"] = args.instance
    try:
        provider_payload = request_launchplane_read(
            service_url=settings["service_url"],
            path=helper_command_path(operation),
            settings=settings,
            query=query,
            timeout=args.timeout,
        )
        if any(str(key) not in PRIVATE_HEALTH_ENDPOINT_LIST_FIELDS for key in provider_payload):
            raise LaunchplaneSafetyError("unsafe_response_shape")
        records = provider_payload.get("records")
        if not isinstance(records, list) or len(records) > PRIVATE_HEALTH_ENDPOINT_MAX_RECORDS:
            raise LaunchplaneSafetyError("invalid_response")
        status = public_code(provider_payload.get("status"), default="ok")
        payload = base_payload(status=status, operation=operation, request=request)
        payload["result"] = {
            "count": len(records),
            "records": [_project_private_health_endpoint_record(record) for record in records],
        }
        payload["summary"] = {
            "launchplane_status": status,
            "trace_id": public_trace_id(provider_payload.get("trace_id")),
            "recommendation": (
                "Name an active record's endpoint_key in the lane's private_http health check."
            ),
        }
        assert_public_safe_shape(payload["summary"])
        emit(payload)
        return 0
    except urllib.error.HTTPError as exc:
        emit_http_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except LaunchplaneSafetyError as exc:
        emit_safety_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError):
        emit_provider_unavailable(operation=operation, request=request)
        return 1
    except (ValueError, json.JSONDecodeError):
        emit_invalid_response(operation=operation, request=request)
        return 1


# Product promotion: the operator status read and the direct dry-run. Launchplane
# records an accepted dry-run for the identity, evidence fingerprint and bump; no
# helper command sends a live promotion.

PRODUCT_PROMOTION_DESTINATION = "prod"
PRODUCT_PROMOTION_BUMPS = {"patch", "minor", "major"}
PRODUCT_PROMOTION_AVAILABILITY_KEYS = ("direct_dry_run", "workflow_dry_run", "workflow_live")
PRODUCT_PROMOTION_STATUS_FIELDS = (
    "promotion_status",
    "deployment_status",
    "backup_status",
    "source_health_status",
    "destination_health_status",
    "release_status",
)
PRODUCT_PROMOTION_DRY_RUN_RESULT_FIELDS = {
    "product",
    "context",
    "from_instance",
    "to_instance",
    "artifact_id",
    "deploy_reference",
    "source_git_ref",
    "backup_record_id",
    "promotion_record_id",
    "deployment_record_id",
    "inventory_record_id",
    *PRODUCT_PROMOTION_STATUS_FIELDS,
    "release_tag",
    "release_url",
    "target_name",
    "target_id",
    "target_category",
    "provider_id",
    "provider_target_type",
    "dry_run",
    "error_message",
    "evidence_fingerprint",
    "bump",
}
PRODUCT_PROMOTION_FINGERPRINT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{15,127}$")


def _public_promotion_fingerprint(value: object) -> str:
    if not isinstance(value, str) or not PRODUCT_PROMOTION_FINGERPRINT_RE.fullmatch(value):
        raise LaunchplaneSafetyError("invalid_response")
    return value


def _project_promotion_evidence(value: object) -> dict[str, object]:
    """A lane's promotion trust; artifact, image, commit, record ids and detail text are dropped."""
    source = _require_dict(value)
    return {
        "environment": public_identifier(source.get("environment")),
        "deployment_status": _optional_code(source.get("deployment_status")),
        "health_status": _optional_code(source.get("health_status")),
        "runtime_identity_status": _optional_code(source.get("runtime_identity_status")),
        "trust_state": _optional_code(source.get("trust_state")),
        "inventory_updated_at": _optional_text(source.get("inventory_updated_at")),
        "inventory_stale_after": _optional_text(source.get("inventory_stale_after")),
    }


def _project_promotion_availability(value: object) -> dict[str, object]:
    source = _require_dict(value)
    reasons = source.get("disabled_reasons") or []
    if not isinstance(reasons, list):
        raise LaunchplaneSafetyError("invalid_response")
    return {
        "enabled": bool(_optional_bool(source.get("enabled"))),
        "authz_action": _optional_identifier(source.get("authz_action")),
        # Reasons are free text that can name hosts and targets; the count says whether any apply.
        "disabled_reason_count": len(reasons),
        "requires_matching_direct_dry_run": bool(
            _optional_bool(source.get("requires_matching_direct_dry_run"))
        ),
        "requires_confirmation": bool(_optional_bool(source.get("requires_confirmation"))),
    }


def _project_product_promotion_status(provider_payload: dict[str, Any]) -> dict[str, object]:
    """Whether a testing-to-prod promotion could run and its evidence fingerprint. The
    release checklist, artifacts, commits, repository, workflow, live confirmations and
    every free-text reason are dropped."""
    if any(
        str(key) not in {"status", "trace_id", "promotion_status"} for key in provider_payload
    ):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    source = _require_dict(provider_payload.get("promotion_status"))
    release_review = _require_dict(source.get("release_review") or {})
    blockers = release_review.get("blockers") or []
    if not isinstance(blockers, list):
        raise LaunchplaneSafetyError("invalid_response")
    default_bump = source.get("default_bump") or "patch"
    if default_bump not in PRODUCT_PROMOTION_BUMPS:
        raise LaunchplaneSafetyError("invalid_response")
    availability = {
        key: _project_promotion_availability(source.get(key))
        for key in PRODUCT_PROMOTION_AVAILABILITY_KEYS
    }
    projected: dict[str, object] = {
        "product": public_identifier(source.get("product")),
        "context": public_identifier(source.get("context")),
        "base_driver_id": _optional_identifier(source.get("base_driver_id")),
        "source_environment": public_identifier(source.get("source_environment")),
        "destination_environment": public_identifier(source.get("destination_environment")),
        "evidence_fingerprint": _public_promotion_fingerprint(
            source.get("evidence_fingerprint")
        ),
        "default_bump": default_bump,
        "trust_state": _optional_code(source.get("trust_state")),
        "source": _project_promotion_evidence(source.get("source")),
        "destination": _project_promotion_evidence(source.get("destination")),
        "release_review": {
            "required": bool(_optional_bool(release_review.get("required"))),
            "approved": bool(_optional_bool(release_review.get("approved"))),
            "blocker_count": len(blockers),
            "unavailable": bool(release_review.get("unavailable_reason")),
        },
        "availability": availability,
    }
    assert_public_safe_shape(projected)
    return projected


def _project_product_promotion_dry_run(result: object) -> dict[str, object]:
    """The dry-run's per-step statuses; artifacts, commits, record ids, target names and
    ids, release URLs and error text are dropped."""
    source = _require_dict(result)
    if any(str(key) not in PRODUCT_PROMOTION_DRY_RUN_RESULT_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    # This command never sends a live promotion; a live result is not this route's answer.
    dry_run = source.get("dry_run")
    if not isinstance(dry_run, bool) or not dry_run:
        raise LaunchplaneSafetyError("invalid_response")
    bump = source.get("bump")
    if bump not in PRODUCT_PROMOTION_BUMPS:
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "product": public_identifier(source.get("product")),
        "context": public_identifier(source.get("context")),
        "from_instance": public_identifier(source.get("from_instance")),
        "to_instance": public_identifier(source.get("to_instance")),
        "dry_run": True,
        "bump": bump,
        "evidence_fingerprint": _public_promotion_fingerprint(source.get("evidence_fingerprint")),
        **{field: _optional_code(source.get(field)) for field in PRODUCT_PROMOTION_STATUS_FIELDS},
        "error_reported": bool(source.get("error_message")),
    }
    assert_public_safe_shape(projected)
    return projected


def product_promotion_dry_run_body(args: argparse.Namespace) -> dict[str, object]:
    fingerprint = _required_argument(args, "evidence_fingerprint")
    if not PRODUCT_PROMOTION_FINGERPRINT_RE.fullmatch(fingerprint):
        raise ValueError("invalid_evidence_fingerprint")
    _require_idempotency(args)
    return {
        "schema_version": 1,
        "reason": _required_argument(args, "reason"),
        "evidence_fingerprint": fingerprint,
        "bump": args.bump,
    }


def execute_product_promotion_status_read(
    *, args: argparse.Namespace, request: dict[str, object], path: str
) -> int:
    operation = "product-promotion-status-read"
    settings = prepare_operator_settings(args=args, operation=operation, request=request)
    if settings is None:
        return 2
    try:
        provider_payload = request_launchplane_read(
            service_url=settings["service_url"],
            path=path,
            settings=settings,
            query={},
            timeout=args.timeout,
        )
        status = public_code(provider_payload.get("status"), default="ok")
        payload = base_payload(status=status, operation=operation, request=request)
        payload["result"] = _project_product_promotion_status(provider_payload)
        payload["summary"] = {
            "launchplane_status": status,
            "trace_id": public_trace_id(provider_payload.get("trace_id")),
            "recommendation": (
                "Dry-run with product-promotion-dry-run and this evidence_fingerprint while "
                "availability.direct_dry_run.enabled is true."
            ),
        }
        assert_public_safe_shape(payload["summary"])
        emit(payload)
        return 0
    except urllib.error.HTTPError as exc:
        emit_http_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except LaunchplaneSafetyError as exc:
        emit_safety_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError):
        emit_provider_unavailable(operation=operation, request=request)
        return 1
    except (ValueError, json.JSONDecodeError):
        emit_invalid_response(operation=operation, request=request)
        return 1


def positive_decimal_id(value: str) -> str:
    normalized = value.strip()
    if not normalized.isdecimal() or int(normalized) < 1:
        raise argparse.ArgumentTypeError("must be a positive decimal ID")
    return normalized


def _add_reviewed_apply_arguments(parser: argparse.ArgumentParser, *, apply: bool) -> None:
    if not apply:
        parser.set_defaults(idempotency_key="")
        return
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--reviewed-dry-run", action="store_true")
    parser.add_argument("--expected-plan-digest", required=True)
    parser.add_argument(
        "--dry-run-evidence-file",
        required=True,
        help="Private saved JSON output from the reviewed dry-run.",
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute bounded Launchplane operator actions.")
    parser.add_argument("--config", help="Optional private operator JSON config path.")
    parser.add_argument("--env-config", help="Optional private operator .env config path.")
    parser.add_argument("--url", help="Optional Launchplane service URL override.")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout seconds.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "operator-config-diagnostic",
        help="Report private operator credential source presence without printing values.",
    )

    preflight = subparsers.add_parser(
        "product-config-preflight", help="Preflight product-config intent without plaintext."
    )
    preflight.add_argument("--product", required=True)
    preflight.add_argument("--context", required=True)
    preflight.add_argument("--instance", help="Runtime instance hint for destination defaults.")
    preflight.add_argument("--source-url", required=True)
    preflight.add_argument("--reason", required=True)
    preflight.add_argument("--secret-binding", action="append", default=[])
    preflight.add_argument("--destination-context")
    preflight.add_argument("--destination-instance")
    preflight.add_argument("--idempotency-key", default="")

    dry_run = subparsers.add_parser(
        "product-config-dry-run", help="Submit a private product-config dry-run payload."
    )
    dry_run.add_argument("--payload-file", required=True, help="Private local JSON payload file.")
    dry_run.add_argument("--idempotency-key", default="")

    apply = subparsers.add_parser(
        "product-config-apply", help="Submit a reviewed private product-config apply payload."
    )
    apply.add_argument("--payload-file", required=True, help="Private local JSON payload file.")
    apply.add_argument("--idempotency-key", required=True)
    apply.add_argument("--reviewed-dry-run", action="store_true")
    for command in ("product-expected-config-dry-run", "product-expected-config-apply"):
        expected_config = subparsers.add_parser(command, help="Add or remove declared product configuration requirements; never credential values.")
        expected_config.add_argument("--payload-file", required=True, help="Private local JSON metadata file.")
        expected_config.set_defaults(idempotency_key="")
        if command.endswith("-apply"):
            expected_config.add_argument("--idempotency-key", required=True)
            expected_config.add_argument("--reviewed-dry-run", action="store_true")
            expected_config.add_argument("--dry-run-evidence-file", required=True, help="Saved helper output for the exact reviewed metadata.")

    change_impact_dry_run = subparsers.add_parser(
        "change-impact-policy-dry-run",
        help="Submit a private change-impact policy dry-run payload.",
    )
    change_impact_dry_run.add_argument(
        "--payload-file", required=True, help="Private local JSON payload file."
    )
    change_impact_dry_run.add_argument("--idempotency-key", default="")

    change_impact_apply = subparsers.add_parser(
        "change-impact-policy-apply",
        help="Submit a reviewed private change-impact policy apply payload.",
    )
    change_impact_apply.add_argument(
        "--payload-file", required=True, help="Private local JSON payload file."
    )
    change_impact_apply.add_argument("--idempotency-key", required=True)
    change_impact_apply.add_argument("--reviewed-dry-run", action="store_true")
    change_impact_apply.add_argument("--expected-policy-digest", required=True)

    change_impact_read = subparsers.add_parser(
        "change-impact-policy-read",
        help="Read bounded active change-impact policy metadata.",
    )
    change_impact_read.add_argument("--repository-id", required=True)

    merge_train_policy_dry_run = subparsers.add_parser(
        "merge-train-policy-import-dry-run",
        help="Dry-run one private merge-train policy import after active-policy preflight.",
    )
    merge_train_policy_dry_run.add_argument(
        "--payload-file", required=True, help="Private local JSON payload file."
    )
    merge_train_policy_dry_run.add_argument(
        "--expected-current-policy-digest", required=True
    )
    merge_train_policy_dry_run.add_argument("--idempotency-key", default="")

    merge_train_policy_apply = subparsers.add_parser(
        "merge-train-policy-import-apply",
        help="Apply one reviewed private merge-train policy import after active-policy preflight.",
    )
    merge_train_policy_apply.add_argument(
        "--payload-file", required=True, help="Private local JSON payload file."
    )
    merge_train_policy_apply.add_argument(
        "--expected-current-policy-digest", required=True
    )
    merge_train_policy_apply.add_argument(
        "--expected-new-policy-digest", required=True
    )
    merge_train_policy_apply.add_argument("--idempotency-key", required=True)
    merge_train_policy_apply.add_argument("--reviewed-dry-run", action="store_true")
    merge_train_policy_apply.add_argument(
        "--dry-run-evidence-file",
        required=True,
        help="Private saved JSON output from the reviewed merge-train policy dry-run.",
    )

    repository_inventory_read = subparsers.add_parser(
        "repository-inventory-read",
        help="Read bounded current repository inventory metadata.",
    )
    repository_inventory_read.add_argument(
        "--repository-id", required=True, type=positive_decimal_id
    )

    repository_inventory_dry_run = subparsers.add_parser(
        "repository-inventory-dry-run",
        help="Submit a private repository inventory dry-run payload.",
    )
    repository_inventory_dry_run.add_argument(
        "--payload-file", required=True, help="Private local JSON payload file."
    )
    repository_inventory_dry_run.add_argument("--idempotency-key", required=True)

    repository_inventory_apply = subparsers.add_parser(
        "repository-inventory-apply",
        help="Submit a reviewed private repository inventory apply payload.",
    )
    repository_inventory_apply.add_argument(
        "--payload-file", required=True, help="Private local JSON payload file."
    )
    repository_inventory_apply.add_argument("--idempotency-key", required=True)
    repository_inventory_apply.add_argument("--reviewed-dry-run", action="store_true")
    repository_inventory_apply.add_argument("--expected-inventory-digest", required=True)
    repository_inventory_apply.add_argument(
        "--dry-run-evidence-file",
        required=True,
        help="Private saved JSON output from the reviewed inventory dry-run.",
    )

    integration_allowances_read = subparsers.add_parser(
        "integration-allowances-read",
        help="Read a lane's non-production integration allowances.",
    )
    integration_allowances_read.add_argument("--product", required=True)
    integration_allowances_read.add_argument("--context", required=True)
    integration_allowances_read.add_argument("--instance", required=True)

    integration_allowances_dry_run = subparsers.add_parser(
        "integration-allowances-dry-run",
        help="Dry-run a lane's whole integration allowance list from a private payload.",
    )
    integration_allowances_dry_run.add_argument(
        "--payload-file", required=True, help="Private local JSON payload file."
    )
    integration_allowances_dry_run.set_defaults(idempotency_key="")

    integration_allowances_apply = subparsers.add_parser(
        "integration-allowances-apply",
        help="Apply reviewed integration allowances bound to the saved dry-run digest.",
    )
    integration_allowances_apply.add_argument(
        "--payload-file", required=True, help="Private local JSON payload file."
    )
    integration_allowances_apply.add_argument("--idempotency-key", required=True)
    integration_allowances_apply.add_argument("--reviewed-dry-run", action="store_true")
    integration_allowances_apply.add_argument("--expected-plan-digest", required=True)
    integration_allowances_apply.add_argument(
        "--dry-run-evidence-file",
        required=True,
        help="Private saved JSON output from the reviewed integration-allowances dry-run.",
    )

    testing_hold_read = subparsers.add_parser(
        "testing-hold-read",
        help="Read a testing lane's staff-testing hold.",
    )
    for argument in ("--product", "--context", "--instance"):
        testing_hold_read.add_argument(argument, required=True)

    product_environment_read = subparsers.add_parser(
        "product-environment-read",
        help="Read one product environment's current build, runtime identity and health.",
    )
    product_environment_read.add_argument("--product", required=True)
    product_environment_read.add_argument("--environment", required=True)

    product_profile_read = subparsers.add_parser(
        "product-profile-read",
        help="Read a product's Owner, production use and lanes.",
    )
    product_profile_read.add_argument("--product", required=True)

    path_check = subparsers.add_parser(
        "path-check", help="Read every blocker on the caller's testing or promotion path."
    )
    path_check.add_argument("--product", required=True)
    path_check.add_argument("--path", required=True, choices=("testing", "promote"))

    product_activity_read = subparsers.add_parser(
        "product-activity-read",
        help="Read a product's recent deployment, promotion and preview activity.",
    )
    product_activity_read.add_argument("--product", required=True)

    preview_history_read = subparsers.add_parser(
        "preview-history-read",
        help=(
            "Read one preview and its generation history, by --preview-id or by "
            "--context, --repository OWNER/REPO and --pr."
        ),
    )
    preview_history_read.add_argument("--preview-id")
    preview_history_read.add_argument("--context")
    preview_history_read.add_argument("--repository")
    preview_history_read.add_argument("--pr", type=int)

    reconcile_requests_read = subparsers.add_parser(
        "reconcile-requests-read",
        help="Read what the event reconciler last decided for each of a product's targets.",
    )
    reconcile_requests_read.add_argument("--product", required=True)

    product_secret_bindings_read = subparsers.add_parser(
        "product-secret-bindings-read",
        help=(
            "Read a product's runtime secret binding metadata (names, scope, lane, class, "
            "sharing reason and version id); never values."
        ),
    )
    product_secret_bindings_read.add_argument("--product", required=True)

    target_replacement_operation_read = subparsers.add_parser(
        "target-replacement-operation-read",
        help=(
            "Read one Odoo target-replacement (testing or stable deploy) operation's "
            "status, phase, times and redacted error."
        ),
    )
    target_replacement_operation_read.add_argument("--operation-id", required=True)

    target_replacement_plan_read = subparsers.add_parser(
        "target-replacement-plan-read",
        help=(
            "Read an Odoo lane's target-replacement plan (read-only): status, blocker codes "
            "and the env-key names it would deliver or retire."
        ),
    )
    target_replacement_plan_read.add_argument("--product", required=True)
    target_replacement_plan_read.add_argument("--instance", required=True)

    for command, help_text in (
        ("testing-hold-dry-run", "Dry-run setting or lifting a testing lane's staff-testing hold."),
        ("testing-hold-apply", "Apply a reviewed testing hold change bound to the saved dry-run digest."),
    ):
        testing_hold = subparsers.add_parser(command, help=help_text)
        for argument in ("--product", "--context", "--instance"):
            testing_hold.add_argument(argument, required=True)
        direction = testing_hold.add_mutually_exclusive_group(required=True)
        direction.add_argument("--hold", dest="hold", action="store_true")
        direction.add_argument("--lift", dest="hold", action="store_false")
        testing_hold.add_argument(
            "--reason",
            required=True,
            help="The hold's reason when holding; the audit reason when lifting.",
        )
        _add_reviewed_apply_arguments(testing_hold, apply=command.endswith("-apply"))

    for command, help_text in (
        (
            "product-repository-identity-dry-run",
            "Dry-run recording a product's repository identity from tracked inventory.",
        ),
        (
            "product-repository-identity-apply",
            "Apply a reviewed repository identity bound to the saved dry-run digest.",
        ),
    ):
        repository_identity = subparsers.add_parser(command, help=help_text)
        repository_identity.add_argument("--product", required=True)
        repository_identity.add_argument("--reason", required=True)
        _add_reviewed_apply_arguments(repository_identity, apply=command.endswith("-apply"))

    for command, help_text in (
        ("product-owner-dry-run", "Dry-run recording or clearing a product's Client."),
        ("product-owner-apply", "Apply a reviewed Client change bound to the saved dry-run digest."),
    ):
        product_owner = subparsers.add_parser(command, help=help_text)
        product_owner.add_argument("--product", required=True)
        selection = product_owner.add_mutually_exclusive_group(required=True)
        selection.add_argument("--github-login", help="The Client's GitHub user login.")
        selection.add_argument("--clear", action="store_true", help="Remove the recorded Client.")
        product_owner.add_argument("--reason", required=True)
        _add_reviewed_apply_arguments(product_owner, apply=command.endswith("-apply"))

    for command, help_text in (
        (
            "product-image-repository-dry-run",
            "Dry-run moving a product's image repository to its repository-named package.",
        ),
        (
            "product-image-repository-apply",
            "Apply a reviewed image repository move bound to the saved dry-run digest.",
        ),
    ):
        image_repository = subparsers.add_parser(command, help=help_text)
        image_repository.add_argument("--product", required=True)
        image_repository.add_argument(
            "--image-repository",
            required=True,
            help="The untagged ghcr.io/<owner>/<name> package named after the repository.",
        )
        image_repository.add_argument("--reason", required=True)
        _add_reviewed_apply_arguments(image_repository, apply=command.endswith("-apply"))

    production_backup_authority_read = subparsers.add_parser(
        "production-backup-authority-read",
        help="Read a production lane's backup policy and targets without provider coordinates.",
    )
    for argument in ("--product", "--context", "--instance", "--promotion-action"):
        production_backup_authority_read.add_argument(argument, required=True)
    for command, help_text in (
        (
            "production-backup-authority-dry-run",
            "Dry-run a production backup policy and its targets from a private payload.",
        ),
        (
            "production-backup-authority-apply",
            "Apply the reviewed backup authority bound to the saved dry-run digest.",
        ),
    ):
        backup_authority = subparsers.add_parser(command, help=help_text)
        backup_authority.add_argument(
            "--payload-file", required=True, help="Private local JSON payload file."
        )
        _add_reviewed_apply_arguments(backup_authority, apply=command.endswith("-apply"))

    for command, help_text in (
        (
            "dokploy-target-create-compose-dry-run",
            "Dry-run creating a lane's Dokploy compose target from a private payload.",
        ),
        (
            "dokploy-target-create-compose-apply",
            "Create the reviewed Dokploy compose target bound to the saved dry-run digest.",
        ),
    ):
        compose_target = subparsers.add_parser(command, help=help_text)
        compose_target.add_argument(
            "--payload-file", required=True, help="Private local JSON payload file."
        )
        _add_reviewed_apply_arguments(compose_target, apply=command.endswith("-apply"))

    for mode in ("dry-run", "apply"):
        source = subparsers.add_parser(
            f"dokploy-target-complete-compose-source-{mode}",
            help="Complete an empty tracked testing compose's repository source.",
        )
        source.add_argument("--payload-file", required=True, help="Private local JSON payload file.")
        _add_reviewed_apply_arguments(source, apply=mode == "apply")

    private_endpoint_read = subparsers.add_parser(
        "private-health-endpoint-read",
        help="List a lane's private health endpoint keys, status and scope, without URLs.",
    )
    private_endpoint_read.add_argument("--product", required=True)
    private_endpoint_read.add_argument("--context", required=True)
    private_endpoint_read.add_argument("--instance", default="")
    for command, help_text in (
        (
            "private-health-endpoint-dry-run",
            "Dry-run recording a private health endpoint from a private payload.",
        ),
        (
            "private-health-endpoint-apply",
            "Apply the reviewed private health endpoint bound to the saved dry-run digest.",
        ),
    ):
        private_endpoint = subparsers.add_parser(command, help=help_text)
        private_endpoint.add_argument(
            "--payload-file", required=True, help="Private local JSON payload file."
        )
        private_endpoint.add_argument("--reason", required=True)
        _add_reviewed_apply_arguments(private_endpoint, apply=command.endswith("-apply"))

    promotion_status_read = subparsers.add_parser(
        "product-promotion-status-read",
        help="Read whether a product's testing-to-prod promotion could run, and its evidence fingerprint.",
    )
    promotion_status_read.add_argument("--product", required=True)
    promotion_dry_run = subparsers.add_parser(
        "product-promotion-dry-run",
        help="Ask Launchplane to dry-run a testing-to-prod promotion; nothing is backed up or deployed.",
    )
    promotion_dry_run.add_argument("--product", required=True)
    promotion_dry_run.add_argument(
        "--evidence-fingerprint", required=True, help="From product-promotion-status-read."
    )
    promotion_dry_run.add_argument("--bump", choices=sorted(PRODUCT_PROMOTION_BUMPS), default="patch")
    promotion_dry_run.add_argument("--reason", required=True)
    promotion_dry_run.add_argument("--idempotency-key", required=True)

    odoo_addon_settings_dry_run = subparsers.add_parser(
        "odoo-addon-settings-dry-run",
        help="Dry-run an Odoo lane's addon settings from a private payload of binding references.",
    )
    odoo_addon_settings_dry_run.add_argument(
        "--payload-file", required=True, help="Private local JSON payload file."
    )
    odoo_addon_settings_dry_run.set_defaults(idempotency_key="")

    odoo_addon_settings_apply = subparsers.add_parser(
        "odoo-addon-settings-apply",
        help="Apply reviewed Odoo addon settings bound to the saved dry-run digest.",
    )
    odoo_addon_settings_apply.add_argument(
        "--payload-file", required=True, help="Private local JSON payload file."
    )
    odoo_addon_settings_apply.add_argument("--idempotency-key", required=True)
    odoo_addon_settings_apply.add_argument("--reviewed-dry-run", action="store_true")
    odoo_addon_settings_apply.add_argument("--expected-plan-digest", required=True)
    odoo_addon_settings_apply.add_argument(
        "--dry-run-evidence-file",
        required=True,
        help="Private saved JSON output from the reviewed addon-settings dry-run.",
    )

    recovery_dry_run = subparsers.add_parser(
        "generic-web-deploy-recovery-dry-run",
        help="Submit a private generic-web deploy-recovery dry-run payload.",
    )
    recovery_dry_run.add_argument(
        "--payload-file", required=True, help="Private local JSON payload file."
    )
    recovery_dry_run.add_argument("--idempotency-key", required=True)

    recovery_apply = subparsers.add_parser(
        "generic-web-deploy-recovery-apply",
        help="Submit a reviewed private generic-web deploy-recovery apply payload.",
    )
    recovery_apply.add_argument(
        "--payload-file", required=True, help="Private local JSON payload file."
    )
    recovery_apply.add_argument("--idempotency-key", required=True)
    recovery_apply.add_argument("--reviewed-dry-run", action="store_true")
    recovery_apply.add_argument("--expected-recovery-digest", required=True)
    recovery_apply.add_argument(
        "--dry-run-evidence-file",
        required=True,
        help="Private saved JSON output from the reviewed recovery dry-run.",
    )

    controller = subparsers.add_parser(
        "merge-train-controller-run-once", help="Call the merge-train controller once."
    )
    controller.add_argument("--repo", required=True, help="Repository in OWNER/REPO form.")
    controller.add_argument("--base-branch", default="main")
    controller.add_argument("--mutate", action="store_true")
    controller.add_argument("--idempotency-key", default="")
    remediation = subparsers.add_parser(
        "preview-feedback-remediation",
        help="Dry-run or apply one audited managed preview-feedback remediation.",
    )
    remediation.add_argument("--mode", choices=("dry-run", "apply"), required=True)
    remediation.add_argument("--product", required=True)
    remediation.add_argument("--context", required=True)
    remediation.add_argument("--repository", required=True)
    remediation.add_argument("--pull-request-url", required=True)
    remediation.add_argument(
        "--terminal-status",
        choices=("destroyed", "failed", "cleanup_failed", "unsupported", "cleared"),
        required=True,
    )
    remediation.add_argument("--reason", required=True)
    remediation.add_argument("--related-issue", required=True)
    remediation.add_argument("--idempotency-key", required=True)
    remediation.add_argument("--reviewed-dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        if args.command == "operator-config-diagnostic":
            request: dict[str, object] = {"diagnostic": "operator_config"}
            try:
                diagnostic = settings_diagnostic(args)
            except ValueError:
                emit(
                    unavailable_payload(
                        operation=args.command,
                        request=request,
                        status="invalid",
                        code="invalid_config",
                        message="Launchplane operator config is invalid.",
                    )
                )
                return 2
            status = "available" if diagnostic.get("ready") is True else "incomplete"
            payload = base_payload(status=status, operation=args.command, request=request)
            payload["summary"] = diagnostic
            emit(payload)
            return 0
        if args.command == "product-config-preflight":
            request = {
                "product": args.product,
                "context": args.context,
                "mode": "dry_run",
                "secret_binding_count": len(args.secret_binding or ()),
            }
            body = product_config_preflight_body(args)
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command in {"product-expected-config-dry-run", "product-expected-config-apply"}:
            mode = "apply" if args.command.endswith("-apply") else "dry-run"
            body = product_expected_config_payload_body(args, mode=mode)
            request = {
                "mode": mode,
                "product": public_identifier(body["product"]),
                "action": "product_profile.expected_config.apply",
                "payload_source": "private_file",
                "payload_digest": metadata_review_digest(body),
                "removal_requested": bool(
                    body.get("remove_runtime_environment_keys") or body.get("remove_managed_secret_bindings")
                ),
            }
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command == "product-config-dry-run":
            request = {"mode": "dry-run", "payload_source": "private_file"}
            body = product_config_payload_body(args, mode="dry-run")
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command == "product-config-apply":
            request = {"mode": "apply", "payload_source": "private_file"}
            body = product_config_payload_body(args, mode="apply")
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command == "change-impact-policy-dry-run":
            request = {"mode": "dry_run", "payload_source": "private_file"}
            body = change_impact_policy_payload_body(args, mode="dry_run")
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command == "change-impact-policy-apply":
            request = {"mode": "apply", "payload_source": "private_file"}
            body = change_impact_policy_payload_body(args, mode="apply")
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command == "change-impact-policy-read":
            request = {
                "payload_source": "operator_argument",
            }
            return execute_change_impact_policy_read(args=args, request=request)
        if args.command in {
            "merge-train-policy-import-dry-run",
            "merge-train-policy-import-apply",
        }:
            mode = (
                "dry_run"
                if args.command == "merge-train-policy-import-dry-run"
                else "apply"
            )
            body, expected_current_digest, candidate_digest = (
                merge_train_policy_import_body(args, mode=mode)
            )
            request = {
                "mode": mode,
                "payload_source": "private_file",
                "expected_current_policy_sha256": expected_current_digest,
                "candidate_policy_sha256": candidate_digest,
            }
            return execute_merge_train_policy_import(
                args=args,
                operation=args.command,
                request=request,
                body=body,
                expected_current_policy_digest=expected_current_digest,
                expected_candidate_policy_digest=candidate_digest,
            )
        if args.command == "repository-inventory-read":
            request = {
                "payload_source": "operator_argument",
            }
            return execute_repository_inventory_read(args=args, request=request)
        if args.command == "repository-inventory-dry-run":
            _require_idempotency(args)
            body = repository_inventory_payload_body(args, mode="dry_run")
            request = {
                "mode": "dry_run",
                "payload_source": "private_file",
                "payload_digest": metadata_review_digest(body),
                "idempotency_key_fingerprint": (
                    repository_inventory_idempotency_key_fingerprint(args.idempotency_key)
                ),
            }
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command == "repository-inventory-apply":
            body = repository_inventory_payload_body(args, mode="apply")
            request = {
                "mode": "apply",
                "payload_source": "private_file",
                "payload_digest": metadata_review_digest(body),
                "idempotency_key_fingerprint": (
                    repository_inventory_idempotency_key_fingerprint(args.idempotency_key)
                ),
            }
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command == "merge-train-controller-run-once":
            request = {
                "repository": args.repo,
                "base_branch": args.base_branch,
                "mutate": bool(args.mutate),
            }
            body = merge_train_controller_body(args)
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command == "preview-feedback-remediation":
            request = {
                "mode": args.mode,
                "product": args.product,
                "context": args.context,
                "repository": args.repository,
                "pull_request_url": args.pull_request_url,
                "terminal_status": args.terminal_status,
            }
            body = preview_feedback_remediation_body(args)
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command == "integration-allowances-read":
            request = {
                "product": public_identifier(args.product),
                "context": public_identifier(args.context),
                "instance": public_identifier(args.instance),
                "payload_source": "operator_argument",
            }
            return execute_lane_config_read(args=args, operation=args.command, request=request)
        if args.command == "testing-hold-read":
            request = {
                "product": public_identifier(args.product),
                "context": public_identifier(args.context),
                "instance": public_identifier(args.instance),
                "payload_source": "operator_argument",
            }
            return execute_lane_config_read(args=args, operation=args.command, request=request)
        if args.command == "product-environment-read":
            path = _product_read_path(
                args.command, product=args.product, environment=args.environment
            )
            request = {
                "product": public_identifier(args.product),
                "environment": public_identifier(args.environment),
                "payload_source": "operator_argument",
            }
            return execute_product_read(
                args=args, operation=args.command, request=request, path=path
            )
        if args.command == "path-check":
            path = _product_read_path(args.command, product=args.product)
            request = {
                "product": public_identifier(args.product),
                "path": args.path,
                "payload_source": "operator_argument",
            }
            return execute_product_read(
                args=args, operation=args.command, request=request, path=path,
                query={"path": args.path},
            )
        if args.command == "product-profile-read":
            path = _product_read_path(args.command, product=args.product)
            request = {
                "product": public_identifier(args.product),
                "payload_source": "operator_argument",
            }
            return execute_product_read(
                args=args, operation=args.command, request=request, path=path
            )
        if args.command == "product-activity-read":
            path = _product_read_path(args.command, product=args.product)
            request = {
                "product": public_identifier(args.product),
                "payload_source": "operator_argument",
            }
            return execute_product_read(
                args=args, operation=args.command, request=request, path=path
            )
        if args.command in {"reconcile-requests-read", "product-secret-bindings-read"}:
            path = _product_read_path(args.command, product=args.product)
            request = {
                "product": public_identifier(args.product),
                "payload_source": "operator_argument",
            }
            return execute_product_read(
                args=args, operation=args.command, request=request, path=path
            )
        if args.command == "target-replacement-plan-read":
            return execute_target_replacement_plan_read(args=args)
        if args.command == "target-replacement-operation-read":
            path = _product_read_path(args.command, operation_id=args.operation_id)
            request = {
                "operation_id": public_identifier(args.operation_id),
                "payload_source": "operator_argument",
            }
            return execute_product_read(
                args=args, operation=args.command, request=request, path=path
            )
        if args.command == "preview-history-read":
            preview_id = _preview_history_selector(args)
            path = _product_read_path(args.command, preview_id=preview_id)
            request = {
                "preview_id": public_identifier(preview_id),
                "payload_source": "operator_argument",
            }
            return execute_product_read(
                args=args, operation=args.command, request=request, path=path
            )
        if args.command in {"testing-hold-dry-run", "testing-hold-apply"}:
            mode = "apply" if args.command == "testing-hold-apply" else "dry-run"
            body = testing_hold_body(args, mode=mode)
            request = {
                "mode": mode,
                "product": public_identifier(body["product"]),
                "context": public_identifier(body["context"]),
                "instance": public_identifier(body["instance"]),
                "hold": body["hold"],
                "payload_source": "operator_argument",
            }
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command in {
            "product-repository-identity-dry-run",
            "product-repository-identity-apply",
        }:
            mode = "apply" if args.command == "product-repository-identity-apply" else "dry-run"
            body = product_repository_identity_body(args, mode=mode)
            request = {
                "mode": mode,
                "product": public_identifier(body["product"]),
                "payload_source": "operator_argument",
            }
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command in {"product-owner-dry-run", "product-owner-apply"}:
            mode = "apply" if args.command == "product-owner-apply" else "dry-run"
            path = _product_read_path(args.command, product=str(args.product).strip())
            body = product_owner_body(args, mode=mode)
            request = {
                "mode": mode,
                "product": public_identifier(args.product),
                "owner_change": "clear" if body["clear"] else "set",
                "github_login": body["github_login"],
                "payload_source": "operator_argument",
            }
            if mode == "apply":
                return execute_product_owner_apply(args=args, request=request, body=body)
            return execute_post(
                args=args,
                operation=args.command,
                path=path,
                request=request,
                body=body,
            )
        if args.command in {"product-image-repository-dry-run", "product-image-repository-apply"}:
            mode = "apply" if args.command == "product-image-repository-apply" else "dry-run"
            path = _product_read_path(args.command, product=str(args.product).strip())
            body = product_image_repository_body(args, mode=mode)
            request = {
                "mode": mode,
                "product": public_identifier(args.product),
                "image_repository": body["image_repository"],
                "payload_source": "operator_argument",
            }
            if mode == "apply":
                return execute_product_image_repository_apply(
                    args=args, request=request, body=body
                )
            return execute_post(
                args=args,
                operation=args.command,
                path=path,
                request=request,
                body=body,
            )
        if args.command == "production-backup-authority-read":
            request = {
                "product": public_identifier(args.product),
                "context": public_identifier(args.context),
                "instance": public_identifier(args.instance),
                "promotion_action": public_identifier(args.promotion_action),
                "payload_source": "operator_argument",
            }
            return execute_production_backup_authority_read(args=args, request=request)
        if args.command in {
            "production-backup-authority-dry-run",
            "production-backup-authority-apply",
        }:
            mode = "apply" if args.command.endswith("-apply") else "dry-run"
            body, request = production_backup_authority_body(args, mode=mode)
            if mode == "apply":
                return execute_production_backup_authority_apply(
                    args=args, request=request, body=body
                )
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command in {
            "dokploy-target-create-compose-dry-run",
            "dokploy-target-create-compose-apply",
            "dokploy-target-complete-compose-source-dry-run",
            "dokploy-target-complete-compose-source-apply",
        }:
            mode = "apply" if args.command.endswith("-apply") else "dry-run"
            body, request = dokploy_compose_body(args, mode=mode)
            if mode == "apply":
                return execute_dokploy_compose_apply(args=args, request=request, body=body)
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command == "private-health-endpoint-read":
            request = {
                "product": public_identifier(args.product),
                "context": public_identifier(args.context),
                "payload_source": "operator_argument",
            }
            if args.instance:
                request["instance"] = public_identifier(args.instance)
            return execute_private_health_endpoint_read(args=args, request=request)
        if args.command in {
            "private-health-endpoint-dry-run",
            "private-health-endpoint-apply",
        }:
            mode = "apply" if args.command.endswith("-apply") else "dry-run"
            body, request = private_health_endpoint_body(args, mode=mode)
            if mode == "apply":
                return execute_private_health_endpoint_apply(
                    args=args, request=request, body=body
                )
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command == "product-promotion-status-read":
            path = _product_read_path(
                args.command, product=args.product, environment=PRODUCT_PROMOTION_DESTINATION
            )
            request = {
                "product": public_identifier(args.product),
                "environment": PRODUCT_PROMOTION_DESTINATION,
                "payload_source": "operator_argument",
            }
            return execute_product_promotion_status_read(args=args, request=request, path=path)
        if args.command == "product-promotion-dry-run":
            path = _product_read_path(
                args.command, product=args.product, environment=PRODUCT_PROMOTION_DESTINATION
            )
            body = product_promotion_dry_run_body(args)
            request = {
                "mode": "dry-run",
                "product": public_identifier(args.product),
                "environment": PRODUCT_PROMOTION_DESTINATION,
                "bump": body["bump"],
                "evidence_fingerprint": body["evidence_fingerprint"],
                "payload_source": "operator_argument",
            }
            return execute_post(
                args=args,
                operation=args.command,
                path=path,
                request=request,
                body=body,
            )
        if args.command in {"integration-allowances-dry-run", "integration-allowances-apply"}:
            mode = "apply" if args.command == "integration-allowances-apply" else "dry-run"
            body = integration_allowances_body(args, mode=mode)
            request = {
                "mode": mode,
                "product": public_identifier(body["product"]),
                "context": public_identifier(body["context"]),
                "instance": public_identifier(body["instance"]),
                "payload_source": "private_file",
            }
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command in {"odoo-addon-settings-dry-run", "odoo-addon-settings-apply"}:
            mode = "apply" if args.command == "odoo-addon-settings-apply" else "dry-run"
            body = odoo_addon_settings_body(args, mode=mode)
            request = {
                "mode": mode,
                "product": public_identifier(body["product"]),
                "context": public_identifier(body["context"]),
                "instance": public_identifier(body["instance"]),
                "addon": "shopify",
                "payload_source": "private_file",
            }
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command == "generic-web-deploy-recovery-dry-run":
            request = {"mode": "dry_run", "payload_source": "private_file"}
            body = generic_web_deploy_recovery_body(args, mode="dry_run")
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command == "generic-web-deploy-recovery-apply":
            request = {"mode": "apply", "payload_source": "private_file"}
            body = generic_web_deploy_recovery_body(args, mode="apply")
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
    except ValueError as exc:
        code = str(exc) or "invalid_request"
        emit(
            unavailable_payload(
                operation=getattr(args, "command", "unknown"),
                request={},
                status="invalid",
                code=code,
                message="Launchplane write action request is invalid.",
            )
        )
        return 2
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
