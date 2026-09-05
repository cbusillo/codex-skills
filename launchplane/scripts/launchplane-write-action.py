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
from typing import Any

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
    "summary",
    "next_actions",
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
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LaunchplaneSafetyError("invalid_response")
    return value


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
            }
            for key in record_source
        ):
            raise LaunchplaneSafetyError("unsafe_response_shape")
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
    }
    for item in value:
        source = _require_dict(item)
        if any(str(key) not in allowed for key in source):
            raise LaunchplaneSafetyError("unsafe_response_shape")
        result: dict[str, object] = {}
        for key in ("action", "integration", "binding_key"):
            if key in source:
                result[key] = public_identifier(source[key])
        projected.append(result)
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
    for key in ("workflow_run_url", "source_of_truth_url"):
        if key in source:
            projected[key] = public_url(source[key])
    for key in (
        "candidate",
        "landing_plan",
        "stack_collapse_plan",
        "stack_discovery",
        "dry_run_result",
        "error",
        "details",
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


def _project_success_output(operation: str, provider_payload: dict[str, Any]) -> tuple[dict[str, object], dict[str, object]]:
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
    records, result = _project_success_output(operation, provider_payload)
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


def repository_inventory_review_digest(body: dict[str, object]) -> str:
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
    payload_digest = repository_inventory_review_digest(body)
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


def product_config_payload_body(args: argparse.Namespace, *, mode: str) -> dict[str, object]:
    body = read_payload_file(args.payload_file)
    body["mode"] = mode
    if mode == "apply":
        _require_idempotency(args)
        if not args.reviewed_dry_run:
            raise ValueError("reviewed_dry_run_required")
        reason = body.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason_required")
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
                "change-impact-policy-apply",
                "generic-web-deploy-recovery-apply",
                "repository-inventory-apply",
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
                            "verified locally. Read back the active policy before retrying."
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


def positive_decimal_id(value: str) -> str:
    normalized = value.strip()
    if not normalized.isdecimal() or int(normalized) < 1:
        raise argparse.ArgumentTypeError("must be a positive decimal ID")
    return normalized


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
                "payload_digest": repository_inventory_review_digest(body),
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
                "payload_digest": repository_inventory_review_digest(body),
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
