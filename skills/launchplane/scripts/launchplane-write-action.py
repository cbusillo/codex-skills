#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["cryptography==50.0.2"]
# ///
"""Execute bounded Launchplane operator actions with public-safe output."""

from __future__ import annotations

import argparse
import copy
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
from launchplane_contract import BOUNDED_READ_RESPONSE_FIELDS  # noqa: E402
from launchplane_contract import helper_command_path  # noqa: E402
from launchplane_contract import internal_helper_path  # noqa: E402
from launchplane_inventory import project_inventory  # noqa: E402
from launchplane_reply_schema import ReplySchemaError  # noqa: E402
from launchplane_reply_schema import check_controller_reply  # noqa: E402
from launchplane_safety import (  # noqa: E402
    CREDENTIAL_LIKE_WORD_RE,
    LaunchplaneSafetyError,
    assert_public_safe_shape,
    build_launchplane_url,
    is_denied_key,
    public_code,
    public_identifier,
    public_operator_text,
    public_summary_string,
    public_service_text,
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
NAMES_ONLY_READS = {
    "context-runtime-settings-read": "settings",
    "instance-runtime-settings-read": "settings",
    "context-secrets-read": "secrets",
    "instance-secrets-read": "secrets",
}
NAMES_ONLY_MAX_ITEMS = 500
READ_ONLY_OPERATIONS = {
    *NAMES_ONLY_READS,
    "dokploy-inventory-read",
    "merge-train-policy-read",
    "governance-projection-read",
    "repository-inventory-read",
    "integration-allowances-read",
    "testing-hold-read",
    "product-environment-read",
    "product-activity-read",
    "protected-artifacts-read",
    "product-profile-read",
    "path-check",
    "preview-history-read",
    "reconcile-requests-read",
    "generic-web-deploy-recovery-reference-read",
    "product-secret-bindings-read",
    "target-replacement-operation-read",
    "target-replacement-plan-read",
    "production-backup-authority-read",
    "production-backup-preflight-read",
    "backup-worker-bindings-read",
    "private-health-endpoint-read",
    "product-promotion-status-read",
    "workflow-rule-retirement-inspect",
    "workflow-rule-retirement-plan",
    "runtime-setting-removal-dry-run",
    "runtime-setting-removal-readback",
    "disposable-lane-inspect",
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
    "public_hosts",
    "odoo_business_hours",
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
        # Path spelling can differ while an ancestor is the same directory.
        # Start at the parent so the final symlink's location stays meaningful.
        return any(parent.samefile(base) for parent in path.parents)


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


def path_with_resolved_parent(path: Path) -> Path:
    # Resolve ancestor aliases while preserving the final symlink's location.
    return path.parent.resolve(strict=True) / path.name


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


class _ServiceResponse(dict[str, Any]):
    """Keep transport status outside the service's projected JSON fields."""

    def __init__(self, payload: dict[str, Any], http_status: object) -> None:
        super().__init__(payload)
        self.http_status = http_status


class ServiceResponseError(ValueError):
    def __init__(self, diagnostics: dict[str, object]) -> None:
        super().__init__("invalid_response")
        self.response_diagnostics = diagnostics


def service_response_diagnostics(source: object) -> dict[str, object]:
    """Retain each safe diagnostic independently of invalid neighboring fields."""
    if isinstance(source, ServiceResponseError):
        return dict(source.response_diagnostics)
    diagnostics: dict[str, object] = {}
    status = getattr(source, "http_status", None)
    if type(status) is int and 100 <= status <= 599:
        diagnostics["http_status"] = status
    if not isinstance(source, dict):
        return diagnostics
    error = source.get("error")
    candidates = {"trace_id": source.get("trace_id")}
    if isinstance(error, dict):
        candidates["error_code"] = error.get("code")
        candidates["error_reason"] = error.get("reason")
    details = source.get("details")
    if isinstance(details, dict):
        candidates["blocking_deployment_id"] = details.get("blocking_deployment_id")
    for key, value in candidates.items():
        try:
            if key == "blocking_deployment_id":
                projected = public_identifier(value)
                public_summary_string(projected, max_length=128)
            else:
                projected = public_trace_id(value) if key == "trace_id" else public_code(value)
            if projected:
                assert_public_safe_shape(projected)
                diagnostics[key] = projected
        except LaunchplaneSafetyError:
            pass
    return diagnostics


def _read_service_response(response: Any) -> dict[str, Any]:
    status = getattr(response, "status", None)
    try:
        payload = json.loads(response.read().decode("utf-8"))
    except (OSError, http.client.HTTPException) as exc:
        setattr(exc, "http_status", status)
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceResponseError(service_response_diagnostics(_ServiceResponse({}, status))) from exc
    if not isinstance(payload, dict):
        raise ServiceResponseError(service_response_diagnostics(_ServiceResponse({}, status)))
    return _ServiceResponse(payload, status)


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
        return _read_service_response(response)


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
        return _read_service_response(response)


def _request_identifier(value: object) -> str:
    try:
        return public_identifier(value)
    except LaunchplaneSafetyError as exc:
        raise ValueError("invalid_request") from exc


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
        projected["next_action"] = public_service_text(source["next_action"])
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


def _project_product_config_public_hosts(value: object) -> dict[str, object]:
    lists = ("before", "after", "added", "updated", "removed", "unchanged", "read_back_hosts")
    source = _require_exact_fields(value, set(lists) | {
        "plan_digest", "runtime_port", "https", "service_name", "certificate_type", "verified"
    })
    projected: dict[str, object] = {}
    for key in lists:
        hosts = source[key]
        if not isinstance(hosts, list) or len(hosts) > 64:
            raise LaunchplaneSafetyError("invalid_response")
        if not all(isinstance(host, str) for host in hosts):
            raise LaunchplaneSafetyError("invalid_response")
        normalized = hosts
        if any(
            len(host) > 253 or len(host.split(".")) < 2 or host != host.lower()
            or any(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
                   for label in host.split(".")) or host.split(".")[-1].isdigit()
            for host in normalized
        ) or len(set(normalized)) != len(normalized):
            raise LaunchplaneSafetyError("invalid_response")
        projected[key] = normalized
    port = source["runtime_port"]
    if type(port) is not int or not 1 <= port <= 65535 or (
        source["https"] is not True or source["service_name"] != "web"
        or source["certificate_type"] != "none" or type(source["verified"]) is not bool
    ):
        raise LaunchplaneSafetyError("invalid_response")
    if source["read_back_hosts"] != (source["after"] if source["verified"] else []):
        raise LaunchplaneSafetyError("invalid_response")
    projected.update(
        plan_digest=_project_sha256(source["plan_digest"]), runtime_port=port,
        https=True, service_name="web", certificate_type="none", verified=source["verified"],
    )
    # The first validated public host owns the prod base URL; no origin URL is copied.
    projected["resolved_base_url"] = f"https://{source['after'][0]}" if source["after"] else ""
    return projected


CALENDAR_COMPANY_LIMIT = 128
CALENDAR_RETRY_ADVICE = " Retain the original payload and idempotency key; never retry automatically."
CALENDAR_QUALIFICATION_ADVICE = (
    "Inspect and correct company/calendar/endpoint references and current authority. Read back the stored policy before recovery."
)


def _utc_timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise LaunchplaneSafetyError("invalid_response")
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return public_timestamp(parsed.astimezone(UTC).isoformat().replace("+00:00", "Z"))
    except (ValueError, OverflowError):
        raise LaunchplaneSafetyError("invalid_response") from None


def _calendar_reference(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or value != value.strip() or "://" in value or "@" in value:
        raise LaunchplaneSafetyError("invalid_response")
    return "" if allow_empty and not value else public_identifier(value)


def _project_business_hours_policy(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    source = _require_dict(value)
    if source.keys() - {"company_id", "private_endpoint_key"} or "company_id" not in source:
        raise LaunchplaneSafetyError("invalid_response")
    return {
        "company_id": _positive_int(source["company_id"]),
        "private_endpoint_key": _calendar_reference(source.get("private_endpoint_key", ""), allow_empty=True),
    }


def _project_business_hours(value: object) -> dict[str, object]:
    required = {
        "operation",
        "policy_before",
        "policy_after",
        "changed",
        "applied",
        "apply_eligible",
        "effective_when",
        "company_scope",
        "permitted_company_ids",
        "companies",
        "timezone",
        "authority_digest",
        "plan_digest",
    }
    source = _require_dict(value)
    if not required.issubset(source) or source.keys() - required - {"runtime_identity", "company_id", "calendar_id", "window"}:
        raise LaunchplaneSafetyError("invalid_response")
    operation = source["operation"]
    if (
        not isinstance(operation, str)
        or operation not in {"inspect", "set", "clear"}
        or source["effective_when"] != "live"
        or source["company_scope"] != "permitted_company_ids"
    ):
        raise LaunchplaneSafetyError("invalid_response")
    if any(type(source[field]) is not bool for field in ("changed", "applied", "apply_eligible")):
        raise LaunchplaneSafetyError("invalid_response")
    permitted = source["permitted_company_ids"]
    companies = source["companies"]
    if (
        not isinstance(permitted, list)
        or len(permitted) > CALENDAR_COMPANY_LIMIT
        or not isinstance(companies, list)
        or len(companies) > CALENDAR_COMPANY_LIMIT
    ):
        raise LaunchplaneSafetyError("invalid_response")
    permitted = [_positive_int(identifier) for identifier in permitted]
    if len(set(permitted)) != len(permitted):
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        field: source[field] for field in ("operation", "changed", "applied", "apply_eligible", "effective_when", "company_scope")
    }
    projected.update(
        policy_before=_project_business_hours_policy(source["policy_before"]),
        policy_after=_project_business_hours_policy(source["policy_after"]),
        permitted_company_ids=permitted,
    )
    company_results: list[dict[str, object]] = []
    for item in companies:
        company = _require_exact_fields(item, {"company_id", "calendar_id", "label", "label_omitted"})
        identifier = _positive_int(company["company_id"])
        if identifier not in permitted or type(company["label_omitted"]) is not bool:
            raise LaunchplaneSafetyError("invalid_response")
        calendar_id = None if company["calendar_id"] is None else _positive_int(company["calendar_id"])
        label = company["label"]
        try:
            safe_label = public_operator_text(label, max_length=160, redact_denied_markers=True)
        except LaunchplaneSafetyError:
            safe_label = ""
        omitted = company["label_omitted"] or not safe_label or safe_label != label
        company_results.append(
            {
                "company_id": identifier,
                "calendar_id": calendar_id,
                "label": "" if omitted else safe_label,
                "label_omitted": bool(omitted),
            }
        )
    if len({company["company_id"] for company in company_results}) != len(company_results):
        raise LaunchplaneSafetyError("invalid_response")
    projected["companies"] = company_results
    for field in ("company_id", "calendar_id"):
        if field in source:
            projected[field] = None if source[field] is None else _positive_int(source[field])
    for field in ("authority_digest", "plan_digest"):
        projected[field] = "" if source[field] == "" else _project_sha256(source[field])
    if operation != "clear" and (not permitted or not projected["authority_digest"]):
        raise LaunchplaneSafetyError("invalid_response")
    projected["timezone"] = _calendar_reference(source["timezone"], allow_empty=True)
    identity = source.get("runtime_identity")
    projected["runtime_identity"] = None
    if identity is not None:
        identity = _require_dict(identity)
        projected["runtime_identity"] = {
            field: _calendar_reference(identity.get(field))
            for field in ("context", "instance", "deployment_record_id", "artifact_id", "source_git_ref")
        }
    window = source.get("window")
    projected["window"] = None
    if window is not None:
        window = _require_exact_fields(window, {"applies", "blocked_reason", "next_release_at", "timezone"})
        if type(window["applies"]) is not bool or not isinstance(window["blocked_reason"], str):
            raise LaunchplaneSafetyError("invalid_response")
        projected["window"] = {
            "applies": window["applies"],
            "blocked_reason": "",
            "next_release_at": _utc_timestamp(window["next_release_at"]),
            "timezone": _calendar_reference(window["timezone"], allow_empty=True),
        }
    if operation == "inspect":
        if (
            source["applied"]
            or source["apply_eligible"]
            or source["changed"]
            or projected["plan_digest"]
            or projected["policy_before"] != projected["policy_after"]
        ):
            raise LaunchplaneSafetyError("invalid_response")
    elif not source["apply_eligible"] or not projected["plan_digest"] or companies:
        raise LaunchplaneSafetyError("invalid_response")
    if operation == "set" and (
        projected.get("company_id") not in permitted
        or projected.get("calendar_id") is None
        or projected["policy_after"] is None
        or projected["window"] is None
        or not projected["timezone"]
    ):
        raise LaunchplaneSafetyError("invalid_response")
    if operation == "clear" and (projected["policy_after"] is not None or identity is not None):
        raise LaunchplaneSafetyError("invalid_response")
    if source["changed"] != (projected["policy_before"] != projected["policy_after"]):
        raise LaunchplaneSafetyError("invalid_response")
    assert_public_safe_shape(projected)
    return projected


def _calendar_request(body: dict[str, object]) -> dict[str, object]:
    calendar = _require_dict(body["odoo_business_hours"])
    reviewed = {key: value for key, value in body.items() if key != "reviewed_plan_sha256"}
    return {
        "mode": body["mode"],
        "payload_source": "private_file",
        "payload_digest": metadata_review_digest(reviewed),
        **{key: body[key] for key in ("product", "context", "instance")},
        "calendar_operation": calendar["operation"],
        **{key: calendar[key] for key in ("company_id", "expected_calendar_id", "private_endpoint_key") if key in calendar},
    }


def _validate_calendar_request(body: dict[str, object]) -> None:
    allowed = {
        "schema_version",
        "mode",
        "product",
        "context",
        "instance",
        "reason",
        "source_label",
        "confirmation",
        "odoo_business_hours",
        "secrets",
        "disable_secret_bindings",
    }
    if (
        body.keys() - allowed
        or any(field in body and body[field] != [] for field in ("secrets", "disable_secret_bindings"))
        or body.get("instance") != "prod"
    ):
        raise ValueError("standalone_calendar_request_required")
    for field in ("product", "context"):
        if body.get(field) != _calendar_reference(body.get(field)):
            raise ValueError(f"invalid_{field}")
    for field in ("reason", "source_label"):
        value = body.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"invalid_{field}")
    calendar = _require_dict(body["odoo_business_hours"])
    if calendar.keys() - {"operation", "company_id", "expected_calendar_id", "private_endpoint_key"}:
        raise ValueError("invalid_calendar_request")
    operation = calendar.get("operation")
    if not isinstance(operation, str) or operation not in {"inspect", "set", "clear"}:
        raise ValueError("invalid_calendar_operation")
    if operation == "clear":
        if calendar.keys() - {"operation"}:
            raise ValueError("invalid_calendar_request")
    else:
        _calendar_reference(calendar.get("private_endpoint_key", ""), allow_empty=True)
        if calendar.get("company_id") is not None:
            _positive_int(calendar["company_id"])
        if operation == "set":
            _positive_int(calendar.get("company_id"))
            _positive_int(calendar.get("expected_calendar_id"))
        elif "expected_calendar_id" in calendar or body["mode"] == "apply":
            raise ValueError("inspect_is_dry_run_only")


def _calendar_result_matches_request(result: dict[str, Any], request: dict[str, object]) -> None:
    calendar = _require_dict(result.get("odoo_business_hours"))
    if (
        any(result.get(field) != request.get(field) for field in ("product", "context", "instance", "mode"))
        or calendar["operation"] != request.get("calendar_operation")
        or calendar["applied"] != (request["mode"] == "apply")
    ):
        raise LaunchplaneSafetyError("invalid_response")
    if calendar["operation"] == "set":
        expected = {"company_id": request.get("company_id"), "private_endpoint_key": request.get("private_endpoint_key", "")}
        if (
            calendar["policy_after"] != expected
            or calendar.get("company_id") != request.get("company_id")
            or calendar.get("calendar_id") != request.get("expected_calendar_id")
        ):
            raise LaunchplaneSafetyError("invalid_response")
    if calendar["operation"] == "inspect":
        company_ids = {company["company_id"] for company in calendar["companies"]}
        expected_ids = {request["company_id"]} if request.get("company_id") is not None else set(calendar["permitted_company_ids"])
        if company_ids != expected_ids:
            raise LaunchplaneSafetyError("invalid_response")
    identity = calendar.get("runtime_identity")
    if calendar["operation"] != "clear" and (
        not isinstance(identity, dict) or identity["context"] != request["context"] or identity["instance"] != "prod"
    ):
        raise LaunchplaneSafetyError("invalid_response")


def _execute_calendar_apply(args: argparse.Namespace, body: dict[str, object], request: dict[str, object]) -> int:
    def finish(settings: dict[str, Any], provider: dict[str, Any], payload: dict[str, Any]) -> bool:
        calendar = _require_dict(payload["result"]["odoo_business_hours"])
        if calendar["plan_digest"] != body["reviewed_plan_sha256"]:
            return False

        def read_back() -> dict[str, object]:
            response = request_launchplane_read(
                service_url=settings["service_url"],
                path=_product_read_path("product-profile-read", product=cast(str, body["product"])),
                settings=settings,
                query={},
                timeout=args.timeout,
            )
            _require_read_fields("product-profile-read", response)
            profile = _require_dict(response.get("profile"))
            if (
                response.get("status") != "ok"
                or profile.get("product") != body["product"]
                or type(profile.get("schema_version")) is not int
                or profile["schema_version"] < 1
                or any(
                    not isinstance(profile.get(field), str) or not profile[field].strip()
                    for field in ("display_name", "repository", "updated_at", "source")
                )
            ):
                raise LaunchplaneSafetyError("invalid_response")
            projected = _project_product_profile(profile)
            return {field: projected[field] for field in ("product", "odoo_business_hours") if field in projected}

        return attach_read_back(
            payload,
            read=read_back,
            matches=lambda profile: profile.get("odoo_business_hours") == calendar["policy_after"],
            label="stored business-hours policy",
        )

    return execute_verified_apply(
        args=args,
        operation=args.command,
        request=request,
        path=helper_command_path(args.command),
        body=body,
        preflight=lambda _settings: None,
        finish=finish,
        label="stored business-hours policy",
    )


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
        ("public_hosts", _project_product_config_public_hosts),
        ("odoo_business_hours", _project_business_hours),
    ):
        if key in source:
            projected[key] = projector(source[key])
    if "public_hosts" in source and (
        instance != "prod" or source["public_hosts"]["verified"] != (source["mode"] == "apply")
    ):
        raise LaunchplaneSafetyError("invalid_response")
    # Hostnames are validated as DNS names by their typed projector. Generic
    # token heuristics would misclassify legitimate names such as desk-app.example.
    assert_public_safe_shape({key: value for key, value in projected.items() if key != "public_hosts"})
    return projected


def _project_merge_component(value: object, *, include_membership: bool = False) -> dict[str, object]:
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
            if include_membership and key == "entries" and source[key] and all(
                isinstance(entry, dict) and isinstance(entry.get("pull_request_number"), int)
                and not isinstance(entry["pull_request_number"], bool) and entry["pull_request_number"] > 0
                for entry in source[key]
            ):
                projected["pull_request_numbers"] = [entry["pull_request_number"] for entry in source[key]]
    return projected


def _merge_train_pr_number(value: object) -> int:
    number = _nonnegative_int(value)
    if number == 0:
        raise LaunchplaneSafetyError("invalid_response")
    return number


def _project_merge_train_stack_plan(value: object) -> dict[str, object]:
    source = _require_dict(value)
    projected = _project_merge_component(source)
    if "root_pull_request_number" in source:
        projected["root_pull_request_number"] = _merge_train_pr_number(source["root_pull_request_number"])
    if "child_dispositions" in source:
        projected["child_dispositions"] = [
            {
                "pull_request_number": _merge_train_pr_number(_require_dict(child).get("pull_request_number")),
                "expected_head_sha": public_identifier(child.get("expected_head_sha")),
                "status": public_code(child.get("status")),
            }
            for child in source["child_dispositions"]
        ]
    return projected


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
        projected["next_action_detail"] = public_service_text(source["next_action_detail"])
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
    code = source.get("code")
    if not isinstance(code, str):
        raise LaunchplaneSafetyError("invalid_response")
    return {
        "code": public_code(code),
        "message": public_service_text(source.get("message")),
    }


def _project_merge_train_conflict_probe(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    source = _require_dict(value)
    numbers = source.get("pull_request_numbers")
    if not isinstance(numbers, list) or not isinstance(source.get("status"), str):
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "status": public_code(source.get("status")),
        "pull_request_numbers": [_merge_train_pr_number(number) for number in numbers],
    }
    if "held_out" in source:
        if not isinstance(source["held_out"], list):
            raise LaunchplaneSafetyError("invalid_response")
        held_out: list[dict[str, object]] = []
        for value in source["held_out"]:
            entry = _require_dict(value)
            conflicts_with = entry.get("conflicts_with")
            if not isinstance(conflicts_with, list) or not isinstance(entry.get("reason"), str):
                raise LaunchplaneSafetyError("invalid_response")
            held_out.append({
                "pull_request_number": _merge_train_pr_number(entry.get("pull_request_number")),
                "head_sha": public_identifier(entry.get("head_sha")),
                "reason": public_code(entry.get("reason")),
                "conflicts_with": [_merge_train_pr_number(number) for number in conflicts_with],
            })
        projected["held_out"] = held_out
    return projected


def _project_merge_train_readiness(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    source = _require_dict(value)
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


_STRUCTURAL_PROVENANCE_FIELDS = (
    "status", "reason_codes", "effective_base_sha", "effective_base_tree_sha",
    "candidate_sha256", "landing_plan_sha256", "provenance_sha256",
)


def _project_merge_train_structural_provenance(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    source = _require_dict(value)
    projected: dict[str, object] = {}
    for key, item in source.items():
        if key not in _STRUCTURAL_PROVENANCE_FIELDS:
            continue
        if key == "reason_codes":
            projected[key] = _public_code_list(item)
        elif not isinstance(item, str):
            raise LaunchplaneSafetyError("invalid_response")
        elif key == "status":
            projected[key] = public_code(item)
        else:
            projected[key] = public_identifier(item) if item else ""
    return projected


def _project_merge_train_block_result(value: object) -> dict[str, object]:
    source = _require_dict(value)
    if (not isinstance(source.get("train_should_continue"), bool)
        or not isinstance(source.get("pull_request_number"), int)
        or isinstance(source.get("pull_request_number"), bool)):
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "status": public_code(source.get("status")),
        "pull_request_number": _merge_train_pr_number(source.get("pull_request_number")),
        "train_should_continue": source["train_should_continue"],
    }
    for key in ("repository", "base_branch", "blocked_label"):
        if key in source:
            projected[key] = public_identifier(source[key])
    if "detail" in source:
        projected["detail"] = public_service_text(source["detail"])
    return projected


def _project_controller_last_result(value: object) -> dict[str, object]:
    """Project the pass evidence the pinned schema declares; omitted fields stay omitted."""
    source = _require_dict(value)
    assert_public_safe_shape(source)
    projected: dict[str, object] = {}
    for key in (
        "repository", "base_branch", "policy_key", "policy_sha256", "pass_started_at",
        "observed_at", "trace_id", "action", "reason_code", "record_id",
        "source_failed_record_id", "candidate_batch_id", "recovery_kind", "candidate_sha",
        "observed_candidate_sha", "base_sha", "check_retry_of", "infrastructure_retry_of",
        "batch_body_retry_of", "semantic_progress_at",
    ):
        if key in source:
            item = source[key]
            if not isinstance(item, str) or len(item) > 256:
                raise LaunchplaneSafetyError("invalid_response")
            projected[key] = public_identifier(item) if item else ""
    for key in ("candidate_checks_status", "base_checks_status"):
        if key in source:
            if source[key] not in ("not_observed", "unknown", "pending", "pass", "fail"):
                raise LaunchplaneSafetyError("invalid_response")
            projected[key] = source[key]
    if "next_actor" in source:
        if source["next_actor"] not in ("controller", "provider", "repository_agent"):
            raise LaunchplaneSafetyError("invalid_response")
        projected["next_actor"] = source["next_actor"]
    if "reconciliation_required" in source:
        if not isinstance(source["reconciliation_required"], bool):
            raise LaunchplaneSafetyError("invalid_response")
        projected["reconciliation_required"] = source["reconciliation_required"]
    if "next_observation" in source:
        observation = source["next_observation"]
        if not isinstance(observation, str) or len(observation) > 256:
            raise LaunchplaneSafetyError("invalid_response")
        projected["next_observation"] = public_service_text(observation)
    return projected


def _project_merge_train_result(result: object) -> dict[str, object]:
    source = _require_dict(result)
    assert_public_safe_shape(source)
    projected: dict[str, object] = {}
    if "last_controller_result" in source:
        projected["last_controller_result"] = _project_controller_last_result(source["last_controller_result"])
    for key in (
        "repository",
        "base_branch",
        "mode",
        "controller_action",
        "reason_code",
        "candidate_ref_cleanup_status",
        "controller_reconciliation_status",
    ):
        if key in source:
            projected[key] = public_identifier(source[key])
    for key in (
        "active_action", "active_phase", "active_record_id",
        "merge_train_batch_landing_plan_record_id",
        "retired_merge_train_batch_candidate_record_id",
    ):
        if key in source:
            projected[key] = _optional_public_identifier(source[key])
    if "landing_progress" in source:
        projected["landing_progress"] = public_code(source["landing_progress"])
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
    if "block_result" in source:
        projected["block_result"] = _project_merge_train_block_result(source["block_result"])
    if "dry_run_result" in source:
        projected["dry_run_result"] = _project_merge_train_dry_run(source["dry_run_result"])
    if "conflict_probe" in source:
        projected["conflict_probe"] = _project_merge_train_conflict_probe(source["conflict_probe"])
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
            projected[key] = (
                _project_merge_train_stack_plan(source[key]) if key == "stack_collapse_plan"
                else _project_merge_component(source[key], include_membership=key in {"candidate", "landing_plan"})
            )
    assert_public_safe_shape(projected)
    return projected


def _github_actor_login(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}(?:\[bot\])?", value
    ):
        raise LaunchplaneSafetyError("invalid_response")
    public_identifier(value.removesuffix("[bot]"))
    return value


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
            "comment_author_login": _github_actor_login(
                observation.get("comment_author_login") or "unknown"
            ),
            "github_actor_login": _github_actor_login(
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
    source = _require_dict(value)
    _require_read_fields(
        "merge-train-policy-targets-read",
        source,
        required=frozenset({"status", "trace_id", "policy", "targets"}),
    )
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
    # Launchplane's record source is required free text; validate it, never emit it.
    source = record.get("source")
    if not isinstance(source, str) or not source.strip():
        raise LaunchplaneSafetyError("invalid_response")
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
        projected["provider_status"] = public_service_text(provider_status)
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
ODOO_WEBSITE_SETTINGS_RESULT_FIELDS = {
    "product",
    "context",
    "instance",
    "record_sha256_before",
    "mode",
    "plan_sha256",
    "before",
    "after",
    "changed",
    "applied",
    "read_back_matches",
    "activation",
}
ODOO_WEBSITE_SETTINGS_PAYLOAD_FIELDS = {
    "product",
    "context",
    "instance",
    "reason",
    "website_settings",
}
ODOO_WEBSITE_SETTINGS_INTENT_FIELDS = {"artifact_id", "settings"}
ODOO_WEBSITE_SETTINGS_FIELDS = {
    "operation",
    "website_id",
    "public_origin",
    "public_indexing_enabled",
    "google_analytics_key",
    "cookies_bar",
    "block_third_party_domains",
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
TESTING_HOLD_FIELDS = {"reason", "recorded_by", "recorded_at", "purpose", "rehearsal"}
TESTING_HOLD_PURPOSES = {"staff_testing", "isolation_staging"}
TESTING_HOLD_REHEARSAL_FIELDS = {"binding_sha256", "target_updated_at", "target_source_label"}
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
    if value is None or value == "":
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
        "reason": public_operator_text(source.get("reason"), redact_denied_markers=True),
    }
    if source.get("evidence"):
        projected["evidence"] = public_operator_text(source.get("evidence"), redact_denied_markers=True)
    if source.get("recorded_by"):
        projected["recorded_by"] = public_identifier(source.get("recorded_by"))
    if source.get("recorded_at"):
        projected["recorded_at"] = public_summary_string(source.get("recorded_at"), max_length=64)
    return projected


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
        "reason": public_operator_text(source.get("reason"), redact_denied_markers=True),
        "evidence": public_operator_text(source.get("evidence"), redact_denied_markers=True),
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
        # This plan is review-bound by its service digest, not projected prose.
        "reason": public_operator_text(source.get("reason"), redact_denied_markers=True),
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


def _testing_hold_purpose(value: object) -> str:
    if not isinstance(value, str) or value not in TESTING_HOLD_PURPOSES:
        raise LaunchplaneSafetyError("invalid_testing_hold_purpose")
    return value


def _project_testing_hold(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    source = _require_dict(value)
    if any(str(key) not in TESTING_HOLD_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    projected: dict[str, object] = {"reason": public_operator_text(source.get("reason"), redact_denied_markers=True)}
    if "purpose" in source:
        projected["purpose"] = _testing_hold_purpose(source["purpose"])
    if source.get("rehearsal") is not None:
        rehearsal = _require_exact_fields(source["rehearsal"], TESTING_HOLD_REHEARSAL_FIELDS)
        projected["rehearsal"] = {
            "binding_sha256": _project_sha256(rehearsal.get("binding_sha256")),
            # Producer provenance is free text; legacy target labels may be empty.
            "target_updated_at": public_operator_text(rehearsal["target_updated_at"], redact_denied_markers=True),
            "target_source_label": (
                "" if rehearsal["target_source_label"] == ""
                else public_operator_text(rehearsal["target_source_label"], redact_denied_markers=True)
            ),
        }
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
        "reason": public_operator_text(source.get("reason")),
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
PRODUCT_OPERATIONAL_MAX_ENTRIES = 50
PRODUCT_ACTIVITY_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,79}$")
PREVIEW_MAX_GENERATIONS = 20
PREVIEW_MAX_SOURCES = 10
RECONCILE_MAX_REQUESTS = 50
RECONCILE_MAX_PLAN_LIST_ITEMS = 20
COMMIT_SHA_RE = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


def _product_read_path(command: str, **segments: str) -> str:
    """The local-extension route with each path segment validated and percent-encoded."""
    encoded: dict[str, str] = {}
    for name, value in segments.items():
        if not isinstance(value, str) or not PRODUCT_READ_PATH_SEGMENT_RE.fullmatch(value):
            raise ValueError(f"invalid_{name}")
        encoded[name] = urllib.parse.quote(value, safe="")
    return helper_command_path(command).format(**encoded)


def _optional_text(value: object, *, max_length: int = 64) -> str:
    if value is None or value == "":
        return ""
    return public_summary_string(value, max_length=max_length)


def _optional_response_text(value: object, *, max_length: int = 64) -> str:
    if value is None or value == "":
        return ""
    return public_service_text(value, max_length=max_length)


def _optional_code(value: object) -> str:
    if value is None or value == "":
        return ""
    return public_code(value)


def _optional_identifier(value: object) -> str:
    if value is None or value == "":
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
        "stale_after": _optional_text(source.get("stale_after")),
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
        "runtime_identity_detail": _optional_response_text(
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
                "name": _optional_response_text(check.get("name"), max_length=120),
                "kind": _optional_code(check.get("kind")),
                "enabled": bool(_optional_bool(check.get("enabled"))),
                "probe_effective": _optional_bool(check.get("probe_effective")),
                "incident_eligible": _optional_bool(check.get("incident_eligible")),
                "status": _optional_code(check.get("status")),
                "failure_code": _optional_code(check.get("failure_code")),
                "observed_at": _optional_text(check.get("observed_at")),
                "record_id": _optional_identifier(check.get("record_id")),
                "incident_status": _optional_code(check.get("incident_status")),
                "trust_state": _optional_code(check.get("trust_state")),
                "provenance": _project_product_provenance(check.get("provenance")),
            }
        )
    return {
        "monitoring_intent": _optional_code(source.get("monitoring_intent")),
        "public_incident_eligible": _optional_bool(source.get("public_incident_eligible")),
        "provenance": _project_product_provenance(source.get("provenance")),
        "checks": projected_checks,
        "checks_truncated": len(checks) > PRODUCT_HEALTH_MAX_CHECKS,
        **_project_operational_list(source, "open_incidents", _project_product_incident),
    }


def _project_operational_list(
    source: dict[str, Any], key: str, project: Callable[[dict[str, Any]], dict[str, object]],
) -> dict[str, object]:
    values = source.get(key)
    if values is None:
        return {key: None, f"{key}_count": None, f"{key}_truncated": None}
    if not isinstance(values, list):
        raise LaunchplaneSafetyError("invalid_response")
    return {
        key: [project(_require_dict(value)) for value in values[:PRODUCT_OPERATIONAL_MAX_ENTRIES]],
        f"{key}_count": len(values),
        f"{key}_truncated": len(values) > PRODUCT_OPERATIONAL_MAX_ENTRIES,
    }


def _project_product_incident(source: dict[str, Any]) -> dict[str, object]:
    return {
        "incident_id": _optional_identifier(source.get("incident_id")),
        "check_name": _optional_response_text(source.get("check_name"), max_length=120),
        **{key: _optional_code(source.get(key)) for key in (
            "status", "severity", "failure_code", "failure_layer",
        )},
    }


def _project_topology_warning(source: dict[str, Any]) -> dict[str, object]:
    return {key: _optional_code(source.get(key)) for key in ("code", "scope", "severity")}


def _project_tls_domain(source: dict[str, Any]) -> dict[str, object]:
    domain = source.get("domain_name")
    if not isinstance(domain, str) or len(domain) > 253 or not all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in domain.split(".")
    ):
        raise LaunchplaneSafetyError("invalid_response")
    return {
        "domain_name": public_identifier(domain),
        "status": _optional_code(source.get("status")),
        "trust_state": _optional_code(source.get("trust_state")),
        "provenance": _project_product_provenance(source.get("provenance")),
    }


def _project_product_topology(value: object) -> dict[str, object] | None:
    source = _optional_dict(value)
    if source is None:
        return None
    observed = _optional_dict(source.get("observed"))
    return {
        **_project_operational_list(source, "warnings", _project_topology_warning),
        "observed": None if observed is None else _project_operational_list(
            observed, "tls_domains", _project_tls_domain,
        ),
    }


def _product_other_warning_count(source: dict[str, Any]) -> int | None:
    warnings = source.get("warnings")
    if warnings is None:
        return None
    if not isinstance(warnings, list) or not all(isinstance(warning, str) for warning in warnings):
        raise LaunchplaneSafetyError("invalid_response")
    topology = _optional_dict(source.get("topology"))
    if topology is None or not isinstance(topology.get("warnings"), list):
        return None
    details = [_require_dict(warning).get("detail") for warning in topology["warnings"]]
    if not all(isinstance(detail, str) for detail in details):
        raise LaunchplaneSafetyError("invalid_response")
    return sum(warning not in details for warning in warnings)


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
    source = _require_dict(value)
    projected: dict[str, object] = {
        "product": public_identifier(source.get("product")),
        "environment": public_identifier(source.get("environment")),
        "context": public_identifier(source.get("context")),
        "repository": _optional_identifier(source.get("repository")),
        "driver_id": _optional_identifier(source.get("driver_id")),
        "target": _project_product_target(source.get("target")),
        "health_monitoring": _project_product_health(source.get("health_monitoring")),
        "topology": _project_product_topology(source.get("topology")),
        "other_warning_count": _product_other_warning_count(source),
        "public_ingress": _project_product_public_ingress(source.get("public_ingress")),
        "trust_state": _optional_code(source.get("trust_state")),
        "provenance": _project_product_provenance(source.get("provenance")),
    }
    assert_public_safe_shape(projected)
    return projected


PRODUCT_PROFILE_MAX_LANES = 20
PRODUCT_PRODUCTION_USES = {"unknown", "prelaunch", "live"}
PRODUCT_RELEASE_ON_ACCEPTANCE_MODES = {
    "held", "promote", "promote_with_rollback_drill", "director_standing"
}
GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")


def _client_or_legacy_owner(source: dict[str, object], client_key: str, owner_key: str) -> object:
    """Read a Client field Launchplane may still send under its legacy ``owner`` name.

    Launchplane is renaming these fields (cbusillo/direction#13). Either name is
    accepted; a response that carries both with different values is refused.
    """
    if client_key not in source:
        return source.get(owner_key)
    if owner_key in source and source[owner_key] != source[client_key]:
        raise LaunchplaneSafetyError("invalid_response")
    return source[client_key]


def _project_product_profile(value: object) -> dict[str, object]:
    """Who the product's Client is and how its production is classified; settings,
    secrets, images, URLs, workflows and expected configuration are dropped."""
    source = _require_dict(value)
    client = _client_or_legacy_owner(source, "client", "owner")
    owner = {} if client is None else client
    lanes = [] if source.get("lanes") is None else source.get("lanes")
    if not isinstance(owner, dict) or not isinstance(lanes, list):
        raise LaunchplaneSafetyError("invalid_response")
    production_use = source.get("production_use") or "unknown"
    if production_use not in PRODUCT_PRODUCTION_USES:
        raise LaunchplaneSafetyError("invalid_response")
    release_on_acceptance = source.get("release_on_acceptance", "held")
    if (
        not isinstance(release_on_acceptance, str)
        or release_on_acceptance not in PRODUCT_RELEASE_ON_ACCEPTANCE_MODES
    ):
        raise LaunchplaneSafetyError("invalid_response")
    owner_login = owner.get("github_login") or ""
    if owner_login and (
        not isinstance(owner_login, str) or not GITHUB_LOGIN_RE.fullmatch(owner_login)
    ):
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "product": public_identifier(source.get("product")),
        "display_name": _optional_response_text(source.get("display_name"), max_length=120),
        "driver_id": _optional_identifier(source.get("driver_id")),
        "repository": _optional_identifier(source.get("repository")),
        "production_use": production_use,
        "release_on_acceptance": release_on_acceptance,
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
    if "odoo_business_hours" in source:
        projected["odoo_business_hours"] = _project_business_hours_policy(source["odoo_business_hours"])
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
    event = event_value
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
            lambda value: public_service_text(value, max_length=max_length),
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



PROTECTED_ARTIFACTS_MAX_ENTRIES = 100
PROTECTED_ARTIFACTS_MAX_WARNINGS = 50



def _protected_artifact_identifier(value: object) -> str:
    artifact_id = _project_artifact_reference(value)
    if "@" in artifact_id:
        name, digest = artifact_id.rsplit("@", 1)
        if "@" in name or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise LaunchplaneSafetyError("invalid_response")
    return artifact_id


def _protected_artifact_warning(value: object) -> str:
    if isinstance(value, str):
        for reference in value.split():
            if "@" in reference and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", reference):
                _protected_artifact_identifier(reference)
    return public_operator_text(value, redact_denied_markers=True)


def _protected_artifact_lane(value: object, *, optional: bool = False) -> str:
    name = _optional_identifier(value) if optional else public_identifier(value)
    if "@" in name:
        raise LaunchplaneSafetyError("invalid_response")
    return name


def _protected_artifact_digest(value: object) -> str:
    digest = _optional_identifier(value)
    if digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise LaunchplaneSafetyError("invalid_response")
    return digest

def _project_protected_artifacts(value: object) -> dict[str, object]:
    source = _require_dict(value)
    entries = source.get("entries")
    warnings = source.get("warnings")
    if not isinstance(entries, list) or not isinstance(warnings, list):
        raise LaunchplaneSafetyError("invalid_response")
    projected_entries: list[dict[str, object]] = []
    for value in entries[:PROTECTED_ARTIFACTS_MAX_ENTRIES]:
        entry = _require_dict(value)
        projected_entries.append({
            "reason": public_code(entry.get("reason")),
            "context": _protected_artifact_lane(entry.get("context")),
            "instance": _protected_artifact_lane(entry.get("instance"), optional=True),
            "artifact_id": _protected_artifact_identifier(entry.get("artifact_id")),
            "source_record_type": public_code(entry.get("source_record_type")),
            "source_record_id": _protected_artifact_identifier(
                public_identifier(entry.get("source_record_id"))
            ),
            "image_digest": _protected_artifact_digest(entry.get("image_digest")),
        })
    projected = {
        "product": _protected_artifact_lane(source.get("product")),
        "context": _protected_artifact_lane(source.get("context"), optional=True),
        "entries": projected_entries,
        "entry_count": len(entries),
        "entries_truncated": len(entries) > PROTECTED_ARTIFACTS_MAX_ENTRIES,
        "warnings": [_protected_artifact_warning(item) for item in warnings[:PROTECTED_ARTIFACTS_MAX_WARNINGS]],
        "warning_count": len(warnings),
        "warnings_truncated": len(warnings) > PROTECTED_ARTIFACTS_MAX_WARNINGS,
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


def _public_commit_sha(value: object) -> str:
    """A full Git commit id; anything else, such as a URL in a malformed record, fails."""
    if not isinstance(value, str) or not COMMIT_SHA_RE.fullmatch(value):
        raise LaunchplaneSafetyError("invalid_response")
    return value


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
            "deploy_operation_status",
            "deploy_status",
            "post_deploy_status",
            "last_failed_error_code",
            "preview_result_error_code",
            "current_build_evidence",
            "testing_deploy_recovery_refusal",
        ),
        _public_dotted_code,
    )
    fields.update(
        dict.fromkeys(
            (
                "context",
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
                "deployment_record_id",
                "last_failed_operation_id",
                "hold_recorded_by",
                "preview_build_run",
                "feedback_recovery_delivery_id",
                "failure_trace_id",
                "failed_restore_deployment_record_id",
            ),
            public_identifier,
        )
    )
    fields.update(
        dict.fromkeys(
            ("head_sha", "current_head_sha", "desired_commit", "current_commit"),
            _public_commit_sha,
        )
    )
    fields.update(
        detail=lambda value: public_service_text(value, max_length=400),
        # Launchplane's fixed description plus up to 32 validated env-key names
        # (launchplane#2717); the helper's own summary redaction still applies.
        last_failed_error_summary=lambda value: public_service_text(value, max_length=1500),
        hold_reason=lambda value: public_operator_text(value, max_length=300, redact_denied_markers=True),
        hold_recorded_at=lambda value: public_summary_string(value, max_length=64),
        preview_url=_public_origin_url,
        testing_runtime_drift_sha256=_project_sha256,
    )
    return fields


RECONCILE_PLAN_VALIDATORS = _reconcile_plan_validators()
RECONCILE_PLAN_FLAGS = frozenset({
    "held",
    "owner_review_requested",
    "current_commit_seen",
    "preview_mutation_attempted",
    "preview_execution_refused",
    "preview_transport_retryable",
    "feedback_recovery_closed_observed",
    "held_reservations_truncated",
})
# Counts: testing build selection (how much run and commit history was read) and retries.
RECONCILE_PLAN_COUNTS = frozenset({
    "pull_request_number",
    "build_runs_seen",
    "build_runs_total",
    "commit_history_seen",
    "built_commits_seen",
    "ordered_built_commits",
    "destroy_failed_attempts",
    "feedback_recovery_failed_attempts",
})
# Written by the reconciler but deliberately left out, and counted under their own
# name: a retry fingerprint is no evidence, a recovery plan carries provider topology,
# and the stop-reason sentences are prose whose facts `reason` and the attempt counts
# already carry.
RECONCILE_PLAN_WITHHELD = frozenset({
    "destroy_retry_key",
    "preview_recovery_plan",
    "destroy_retry_stop_reason",
    "feedback_recovery_stop_reason",
    "artifact_scan",
    "held_reservations",
    "testing_deploy_recovery",
    "product",
})
# Plan lists of setting key names (never values), by the name they are output under.
RECONCILE_PLAN_KEY_NAME_LISTS = {
    "omitted_integration_credential_keys": "omitted_integration_keys",
    "missing_keys": "missing_keys",
}


def _reconcile_deploy_key_digest(value: object) -> str:
    if not isinstance(value, str) or len(value) > 1024:
        raise LaunchplaneSafetyError("invalid_response")
    return hashlib.sha256(value.encode()).hexdigest()


def _project_rejected_builds(entries: list[object], drops: _FieldDrops) -> list[dict[str, object]]:
    validators = {"commit": _public_commit_sha, "run_id": _positive_int, "code": _public_dotted_code}
    projected: list[dict[str, object]] = []
    for entry in entries[:RECONCILE_MAX_PLAN_LIST_ITEMS]:
        if not isinstance(entry, dict):
            drops.drop("requests[].last_plan.rejected_builds[]")
            continue
        evidence: dict[str, object] = {}
        for key, value in entry.items():
            if key in validators:
                evidence[key] = (
                    None
                    if key == "run_id" and value is None
                    else drops.keep(f"requests[].last_plan.rejected_builds[].{key}", validators[key], value)
                )
            else:
                drops.drop(
                    "requests[].last_plan.rejected_builds[].error"
                    if key == "error"
                    else "requests[].last_plan.rejected_builds[].<unlisted field>"
                )
        if evidence.get("commit"):
            projected.append({key: value for key, value in evidence.items() if value != ""})
        else:
            drops.drop("requests[].last_plan.rejected_builds[]")
    if len(entries) > RECONCILE_MAX_PLAN_LIST_ITEMS:
        drops.drop("requests[].last_plan.rejected_builds[]")
    return projected


def _project_reconcile_feedback(value: object, drops: _FieldDrops) -> dict[str, object]:
    if not isinstance(value, dict):
        drops.drop("requests[].last_plan.pr_feedback")
        return {}
    validators = {
        "status": _public_dotted_code,
        "delivery_status": _public_dotted_code,
        "delivery_action": _public_dotted_code,
        "owner_review": _public_dotted_code,
        "feedback_id": public_identifier,
        "pull_request_number": _nonnegative_int,
        "comment_id": _nonnegative_int,
        "body_sha256": _protected_artifact_digest,
    }
    projected: dict[str, object] = {}
    for key, item in value.items():
        if key in validators:
            kept = drops.keep(f"requests[].last_plan.pr_feedback.{key}", validators[key], item)
            if kept != "":
                projected[key] = kept
        elif key != "error" or (item is not None and item != ""):
            path = "error" if key == "error" else "<unlisted field>"
            drops.drop(f"requests[].last_plan.pr_feedback.{path}")
    return projected


def _project_reconcile_plan(plan_value: object, drops: _FieldDrops) -> dict[str, object]:
    """The plan fields the reconciler is known to write. Any other field, and a value
    that does not validate, is dropped and counted."""
    plan = plan_value if isinstance(plan_value, dict) else {}
    projected: dict[str, object] = {}
    for key, value in plan.items():
        validate = RECONCILE_PLAN_VALIDATORS.get(key)
        if validate is not None:
            projected[key] = drops.keep(f"requests[].last_plan.{key}", validate, value)
        elif key == "deploy_idempotency_key":
            projected["deploy_key_sha256"] = drops.keep(
                "requests[].last_plan.deploy_key_sha256", _reconcile_deploy_key_digest, value
            )
        elif key in RECONCILE_PLAN_FLAGS:
            projected[key] = value if isinstance(value, bool) else None
        elif key in RECONCILE_PLAN_COUNTS:
            projected[key] = value if type(value) is int else None
        elif key == "rejected_builds" and isinstance(value, list):
            projected[key] = _project_rejected_builds(value, drops)
        elif key == "pr_feedback":
            projected[key] = _project_reconcile_feedback(value, drops)
        elif key in RECONCILE_PLAN_WITHHELD:
            drops.drop(f"requests[].last_plan.{key}")
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
    request = request_value
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
        "last_error": field("last_error", lambda value: public_service_text(value, max_length=400)),
        "last_plan": _project_reconcile_plan(request.get("last_plan"), drops),
    }


def _project_reconcile_requests(
    provider_payload: dict[str, Any], *, target_key: str | None = None
) -> dict[str, object]:
    """Each target's last reconcile decision, bounded. Odd fields are dropped and unusable
    requests omitted, with counts and field paths; a secret-looking value fails the read."""
    requests = provider_payload.get("requests") or []
    if not isinstance(requests, list):
        raise LaunchplaneSafetyError("invalid_response")
    if target_key is not None:
        requests = [
            item for item in requests
            if isinstance(item, dict) and item.get("target_key") == target_key
        ]
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
    result = value
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
    if image_reference is not None and image_reference != "":
        # The repository part of the reference is dropped; only the digest is kept.
        drops.drop("result.image_reference")
    return projected


def _project_target_replacement_operation(provider_payload: dict[str, Any]) -> dict[str, object]:
    """One Odoo target-replacement operation's progress and bounded failure details. Error messages,
    settings, URLs, provider target names and evidence payloads are dropped and listed by
    path; a secret-looking value in a kept field fails the read."""
    operation = _require_dict(provider_payload.get("operation"))
    drops = _FieldDrops()
    raw_result = provider_payload.get("result")
    if raw_result is None:
        raw_result = operation.get("result")

    def field(field_name: str, validate: Any) -> object:
        return drops.keep(f"operation.{field_name}", validate, operation.get(field_name))

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
        "error_description": field("error_description", public_service_text),
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
    target = value
    projected: dict[str, object] = {
        name: _project_env_key_names(target.get(name), path=f"plan.current_target.{name}", drops=drops)
        for name in TARGET_REPLACEMENT_CURRENT_TARGET_KEY_LISTS
    }
    deployment_status = target.get("latest_deployment_status")
    if isinstance(deployment_status, str):
        deployment_status = deployment_status.strip()
        if CREDENTIAL_LIKE_WORD_RE.search(deployment_status):
            raise LaunchplaneSafetyError("unsafe_response_shape")
    projected["latest_deployment_status"] = (
        drops.keep("plan.current_target.latest_deployment_status", public_code, deployment_status)
        or None
    )
    _report_dropped_fields(
        target,
        prefix="plan.current_target",
        kept=set(projected),
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
    generation = generation_value
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
            "failure_summary", lambda value: public_service_text(value, max_length=300)
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


SERVICE_RUNTIME_SETTING_PLAN_FIELDS = frozenset({
    "status", "actor", "reason", "mode", "key", "value_sha256", "plan_digest",
    "trace_id", "runtime_environment",
})
SERVICE_RUNTIME_SETTING_KEY_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")


def _service_runtime_setting_key(value: object) -> str:
    if not isinstance(value, str) or SERVICE_RUNTIME_SETTING_KEY_RE.fullmatch(value) is None:
        raise LaunchplaneSafetyError("invalid_response")
    return value


def _project_service_runtime_setting_plan(
    result: object, *, replayed: object = None
) -> dict[str, object]:
    source = _require_dict(result)
    if any(str(key) not in SERVICE_RUNTIME_SETTING_PLAN_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    runtime_environment = _project_runtime_environment(source.get("runtime_environment"))
    if runtime_environment.get("scope") != "context" or runtime_environment.get("context") != "launchplane":
        raise LaunchplaneSafetyError("invalid_response")
    if replayed is not None and not isinstance(replayed, bool):
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "status": public_code(source.get("status")),
        "mode": _reviewed_plan_mode(source.get("mode")),
        "key": _service_runtime_setting_key(source.get("key")),
        "value_sha256": _project_sha256(source.get("value_sha256")),
        "plan_digest": _project_sha256(source.get("plan_digest")),
        "trace_id": public_trace_id(source.get("trace_id")),
        "runtime_environment": runtime_environment,
    }
    if replayed:
        projected["replayed"] = True
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
        "reason": public_operator_text(source.get("reason"), redact_denied_markers=True),
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
        "reason": public_operator_text(source.get("reason"), redact_denied_markers=True),
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
    projected["next_actions"] = [public_service_text(item) for item in next_actions]
    assert_public_safe_shape(projected)
    return projected


def _project_odoo_website_settings_intent(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    intent = _require_dict(value)
    settings = _require_dict(intent.get("settings"))
    if any(str(key) not in ODOO_WEBSITE_SETTINGS_INTENT_FIELDS for key in intent) or any(
        str(key) not in ODOO_WEBSITE_SETTINGS_FIELDS for key in settings
    ):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    website_id = settings.get("website_id")
    if isinstance(website_id, bool) or not isinstance(website_id, int) or website_id < 1:
        raise LaunchplaneSafetyError("invalid_response")
    analytics_key = settings.get("google_analytics_key", "")
    projected_settings: dict[str, object] = {
        "operation": public_code(settings.get("operation"), default="apply"),
        "website_id": website_id,
        "public_origin": _public_origin_url(settings.get("public_origin")),
        "public_indexing_enabled": bool(_optional_bool(settings.get("public_indexing_enabled"))),
        # A GA4 Measurement ID is published in every page; it is not a secret.
        "google_analytics_key": public_identifier(analytics_key) if analytics_key else "",
    }
    # Collection-only rollback stores null consent controls; keep null distinct from false.
    for key in ("cookies_bar", "block_third_party_domains"):
        projected_settings[key] = _optional_bool(settings.get(key))
    return {
        "artifact_id": public_identifier(intent.get("artifact_id")),
        "settings": projected_settings,
    }


def _project_odoo_website_settings_result(result: object) -> dict[str, object]:
    source = _require_dict(result)
    if any(str(key) not in ODOO_WEBSITE_SETTINGS_RESULT_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    projected: dict[str, object] = {
        "mode": public_code(source.get("mode")),
        "product": public_identifier(source.get("product")),
        "context": public_identifier(source.get("context")),
        "instance": public_identifier(source.get("instance")),
        "record_sha256_before": _optional_sha256(source.get("record_sha256_before")),
        "plan_sha256": _optional_sha256(source.get("plan_sha256")),
        "before": _project_odoo_website_settings_intent(source.get("before")),
        "after": _project_odoo_website_settings_intent(source.get("after")),
        "activation": public_service_text(source.get("activation")),
    }
    for key in ("changed", "applied", "read_back_matches"):
        projected[key] = bool(_optional_bool(source.get(key)))
    assert_public_safe_shape(projected)
    return projected


def _project_success_output(
    operation: str, provider_payload: dict[str, Any], *, request: dict[str, object] | None = None
) -> tuple[dict[str, object], dict[str, object]]:
    if operation in {
        "generic-web-deploy-recovery-dry-run",
        "generic-web-deploy-recovery-apply",
    }:
        result = _project_generic_web_deploy_recovery_result(
            provider_payload,
            operation=operation,
        )
        if request and "recovery_request_sha256" in request and (
            result["product"] != request["product"] or result["instance"] != request["instance"]
        ):
            raise LaunchplaneSafetyError("invalid_response")
        if request and operation == "generic-web-deploy-recovery-apply" and (
            ("expected_recovery_digest" in request and result["recovery_digest"] != request["expected_recovery_digest"])
            or ("reviewed_recovery_action" in request and result["recovery_action"] != request["reviewed_recovery_action"])
        ):
            raise LaunchplaneSafetyError("invalid_response")
        return {}, result
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
        # summarize_success already removed every field the pinned schema
        # does not declare.
        records = {
            key: public_identifier(value)
            for key, value in _require_dict(provider_payload.get("records")).items()
        }
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
        projected_config = _project_product_config_apply_result(result)
        if "public_hosts" in projected_config and projected_config.get("mode") != (
            "apply" if operation == "product-config-apply" else "dry-run"
        ):
            raise LaunchplaneSafetyError("invalid_response")
        if "odoo_business_hours" in projected_config or request.get("calendar_operation"):
            _calendar_result_matches_request(projected_config, request)
        return records, projected_config
    if operation == "preview-feedback-remediation":
        result = _require_dict(provider_payload.get("result"))
        if (
            provider_payload.get("status") != "accepted"
            or request.get("mode") not in {"apply", "dry-run"}
            or result.get("mode") != request.get("mode")
        ):
            raise LaunchplaneSafetyError("invalid_response")
        records = _project_records(
            provider_payload.get("records"),
            {
                "preview_pr_feedback_remediation_id",
                "preview_pr_feedback_id",
            },
        )
        return records, _project_preview_feedback_remediation_result(
            result
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
    if operation in {"odoo-website-settings-dry-run", "odoo-website-settings-apply"}:
        records = _project_records(
            provider_payload.get("records"), {"product_profile", "context", "instance"}
        )
        return records, _project_odoo_website_settings_result(provider_payload.get("result"))
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
    if operation in {"health-monitoring-dry-run", "health-monitoring-apply"}:
        records = _project_records(
            provider_payload.get("records"), {"product_profile", "context", "instance", "health_check"}
        )
        return records, _project_health_monitoring_plan(
            provider_payload.get("result"), request=request or {}
        )
    if operation in {"product-repository-identity-dry-run", "product-repository-identity-apply"}:
        records = _project_records(
            provider_payload.get("records"), {"product_profile", "repository_inventory"}
        )
        return records, _project_product_repository_identity_plan(provider_payload.get("result"))
    if operation in {"service-runtime-setting-dry-run", "service-runtime-setting-apply"}:
        records = _project_records(provider_payload.get("records"), {"runtime_environment"})
        return records, _project_service_runtime_setting_plan(
            provider_payload.get("result"), replayed=provider_payload.get("replayed")
        )
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
    if operation in DOKPLOY_COMPOSE_DOMAIN_COMMANDS:
        return _project_records(provider_payload.get("records"), set()), _project_compose_domain_setup(
            provider_payload.get("result"), request=request or {}
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
    if operation == DOKPLOY_APPLICATION_READBACK_COMMAND:
        records = _project_records(provider_payload.get("records"), set())
        return records, _project_dokploy_application_readback(
            provider_payload.get("result"), request=request
        )
    if operation in DOKPLOY_APPLICATION_COMMANDS or operation == DOKPLOY_APPLICATION_RETAIN_COMMAND:
        records = _project_records(provider_payload.get("records"), set())
        return records, _project_dokploy_application_setup(
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


def summarize_repository_inventory_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    _require_read_fields("repository-inventory-read", provider_payload)
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


def _require_read_fields(
    operation: str, provider_payload: dict[str, Any], *, required: frozenset[str] = frozenset()
) -> None:
    keys = set(map(str, provider_payload))
    if keys.difference(BOUNDED_READ_RESPONSE_FIELDS[operation]) or required.difference(keys):
        raise LaunchplaneSafetyError("unsafe_response_shape")


def _require_exact_fields(value: object, fields: set[str]) -> dict[str, Any]:
    source = _require_dict(value)
    if set(map(str, source)) != fields:
        raise LaunchplaneSafetyError("unsafe_response_shape")
    return source


def _error_payload_from_http(exc: urllib.error.HTTPError) -> dict[str, object]:
    try:
        raw = exc.read().decode()
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, http.client.HTTPException, OSError):
        parsed = {}
    return _ServiceResponse(parsed if isinstance(parsed, dict) else {}, exc.code)


def _status_for_http_error(code: int, provider_payload: dict[str, object]) -> str:
    from launchplane_runtime_setting_removal import STALE_CODES as runtime_setting_stale_codes
    from launchplane_workflow_rule_retirement import STALE_CODES as workflow_rule_stale_codes

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
    if error_code in {"mutation_reconciliation_required", "reconciliation_required"}:
        return "reconciliation_required"
    if error_code == "provider_mutation_failed":
        return "failed"
    if code == 404 and error_code:
        return "not_found"
    if error_code in {
        "stale",
        "mismatched_intent",
        "matching_dry_run_required",
        "stale_plan",
        "stale_review",
        "stale_recovery_digest",
    } | runtime_setting_stale_codes | workflow_rule_stale_codes:
        return "stale"
    if code == 409:
        return "conflict" if error_code in {
            "idempotency_key_reused", "idempotency_conflict", "restart_target_busy",
            "target_busy", "odoo_stable_target_replacement_operation_active",
        } else "refused"
    return "unavailable"


def http_error_recommendation(status: str) -> str:
    recommendations = {
        "merge_train_target_not_ready": (
            "Use gh-rulesets.py plan/apply for the standard ruleset pair and "
            "gh-rulesets.py checks-plan/checks-apply/checks-readback to require CI checks "
            "on OWNER/REPO. Follow skills/github/references/cli-reference.md#required-status-checks "
            "and README.md#standard-repository-rulesets for complete arguments, saved-plan "
            "review and existing approval requirements. Read the template's enqueue label "
            "with merge-train-policy-read --repo OWNER/ENROLLED and create that label on "
            "OWNER/REPO. Then propose enrollment again."
        ),
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
        "refused": "Stop this action and inspect the refusal and trace. Correct the prerequisite under existing authority; a refusal does not authorize another dispatch or route.",
        "conflict": "Inspect the existing request or blocking operation. Preserve its original arguments and idempotency key; do not change keys to bypass a conflict.",
        "reconciliation_required": "Preserve the original request, reviewed evidence and idempotency key. Read its original operation and use supported reconciliation; do not dispatch another operation.",
        "failed": "Read the original operation's terminal result and rollback evidence. This failure does not authorize another dispatch or a new key.",
        "not_found": "The requested resource or route was not found. Confirm the exact reference and deployed capability; this is not recovery clearance or authority to dispatch.",
        "unavailable": "Launchplane service was unavailable or returned an invalid error envelope. Retain the original request, evidence and key; inspect its outcome before any supported recovery.",
    }
    return recommendations.get(status, "Stop and surface the compact Launchplane error.")


def summarize_success(
    *, operation: str, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    unrecognized: list[str] = []
    if operation == "merge-train-controller-run-once":
        try:
            checked, unrecognized = check_controller_reply(provider_payload)
        except ReplySchemaError as exc:
            raise LaunchplaneSafetyError(exc.code) from exc
        # Fields newer than the pin are never projected, but what Launchplane
        # sent must still be safe to have read.
        assert_public_safe_shape(provider_payload.get("records"))
        assert_public_safe_shape(provider_payload.get("result"))
        provider_payload = checked
    try:
        records, result = _project_success_output(operation, provider_payload, request=request)
    except TypeError as exc:
        raise LaunchplaneSafetyError("invalid_response") from exc
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
        summary.update(_controller_error_diagnostics(provider_payload))
        if unrecognized:
            # Newer than this skill's pinned schema: dropped from the result, not a failure.
            summary["unrecognized_fields"] = ",".join(unrecognized)
            cast(list[object], payload["warnings"]).append(warning(
                "controller_reply_newer_than_pinned_schema",
                "Launchplane sent reply fields this skill's pinned schema does not declare; "
                "they were left out. Refresh the pin with sync-merge-train-reply-schema.py.",
            ))
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
        elif operation in {"health-monitoring-dry-run", "health-monitoring-apply"}:
            summary["plan_sha256"] = result.get("plan_sha256")
            summary["recommendation"] = (
                "Save and review this dry-run output, then apply the same arguments with "
                "--expected-plan-digest."
                if operation.endswith("dry-run")
                else "Check read_back_matches and endpoint_preserved before relying on the policy."
            )
        elif operation in {"testing-hold-dry-run", "testing-hold-apply"}:
            summary["plan_sha256"] = result.get("plan_sha256")
            summary["recommendation"] = (
                "Save this redacted dry-run output, review the hold change, then apply the same "
                "product, lane, hold direction, purpose and reason with --expected-plan-digest."
                if operation == "testing-hold-dry-run"
                else "Check read_back_matches and reconcile_requested. A staff-testing lift requests "
                "testing reconciliation; an isolation-staging lift alone does not."
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
        elif operation in {"service-runtime-setting-dry-run", "service-runtime-setting-apply"}:
            summary["plan_digest"] = result.get("plan_digest")
            summary["recommendation"] = (
                "Save this redacted dry-run output, review the key and value digest, then apply "
                "the same key, value and reason with --expected-plan-digest."
                if operation == "service-runtime-setting-dry-run"
                else "Read the key back with context-runtime-settings-read --context launchplane."
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
        elif operation in DOKPLOY_COMPOSE_DOMAIN_COMMANDS:
            summary["plan_sha256"] = result.get("plan_sha256")
            summary["recommendation"] = (
                "Save and review this dry-run, then apply the exact private payload with "
                "--expected-plan-digest and the saved evidence."
                if operation.endswith("-dry-run")
                else "Check read_back_matches, then verify the network path separately."
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
        elif operation in DOKPLOY_APPLICATION_COMMANDS:
            summary["plan_sha256"] = result.get("plan_sha256")
            summary["recommendation"] = (
                "Save this redacted dry-run output and review it, then apply the exact same "
                "private payload with --expected-plan-digest. Never re-run the dry run to retry: "
                "keep this evidence and the first apply's --idempotency-key for every retry."
                if operation.endswith("-dry-run")
                else "Check read_back_matches and allocation_state before relying on the target. "
                "Retry only with the original key and evidence."
            )
        elif operation == DOKPLOY_APPLICATION_READBACK_COMMAND:
            summary["plan_sha256"] = result.get("plan_sha256")
            summary["recommendation"] = (
                "Readback created and adopted nothing. Save this output. If the allocation cannot "
                "resume under the original key, retain it with this custody_sha256, the same "
                "payload, evidence and original key."
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
        elif operation in {"odoo-website-settings-dry-run", "odoo-website-settings-apply"}:
            summary["plan_sha256"] = result.get("plan_sha256")
            summary["recommendation"] = (
                "Save this redacted dry-run output, review before and after, then apply the exact "
                "same private payload with --expected-plan-digest."
                if operation == "odoo-website-settings-dry-run"
                else "Check read_back_matches. The intent is inert until the Client accepts the "
                "release; that acceptance applies it to the Odoo database."
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
                else _recovery_apply_readback(request)
            )
    assert_public_safe_shape(summary)
    payload["summary"] = {key: value for key, value in summary.items() if value not in {"", None}}
    return payload


def _controller_error_diagnostics(provider_payload: dict[str, Any]) -> dict[str, object]:
    """Retain independently validated identifiers even when the rest is unprojectable."""
    diagnostics: dict[str, object] = {}
    details = provider_payload.get("details")
    number = details.get("pull_request_number") if isinstance(details, dict) else None
    if isinstance(number, int) and not isinstance(number, bool) and number > 0:
        diagnostics["pull_request_number"] = number
    try:
        trace_id = public_trace_id(provider_payload.get("trace_id"))
        if trace_id:
            diagnostics["trace_id"] = trace_id
    except (LaunchplaneSafetyError, TypeError):
        pass
    result = provider_payload.get("result")
    sources = [provider_payload]
    if isinstance(result, dict):
        sources.append(result)
    for source in sources:
        for key in ("error", "blocking_reason"):
            error = source.get(key)
            if isinstance(error, dict):
                try:
                    code = public_code(error.get("code"))
                    assert_public_safe_shape(code)
                    diagnostics["error_code"] = code
                    return diagnostics
                except (LaunchplaneSafetyError, TypeError):
                    pass
    return diagnostics


def summarize_http_error(
    *, operation: str, request: dict[str, object], exc: urllib.error.HTTPError,
    provider_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    if provider_payload is None:
        provider_payload = _error_payload_from_http(exc)
    status = _status_for_http_error(exc.code, provider_payload)
    payload = base_payload(status=status, operation=operation, request=request)
    if operation == "merge-train-controller-run-once":
        diagnostics = _controller_error_diagnostics(provider_payload)
        payload["summary"] = {
            "http_status": exc.code,
            "error_code": status,
            **diagnostics,
            "recommendation": http_error_recommendation(status),
        }
        payload["warnings"] = [warning(
            cast(str, diagnostics.get("error_code", status)),
            "Launchplane controller request was rejected; inspect the trace before retrying.",
        )]
        return payload
    diagnostics = service_response_diagnostics(provider_payload)
    summary: dict[str, object] = {
        "http_status": exc.code,
        "trace_id": "",
        "error_code": status,
        **diagnostics,
        "recommendation": http_error_recommendation(status),
    }
    error_code = summary["error_code"]
    if operation == "privileged-policy-propose" and exc.code == 422 and error_code == "merge_train_target_not_ready":
        summary["recommendation"] = http_error_recommendation(error_code)
    if (
        request.get("setup_operation") == "create-application"
        and isinstance(error_code, str)
        and error_code in APPLICATION_STATE_VOLUME_NEXT_STEPS
    ):
        summary["recommendation"] = APPLICATION_STATE_VOLUME_NEXT_STEPS[error_code]
    if request.get("calendar_operation") and error_code == "calendar_qualification_unavailable":
        summary["recommendation"] = CALENDAR_QUALIFICATION_ADVICE + CALENDAR_RETRY_ADVICE
    payload["summary"] = summary
    if operation == "privileged-policy-propose" and (exc.code in {404, 409} or 500 <= exc.code < 600):
        payload["status"] = (
            "unsupported" if exc.code == 404 else "conflict" if exc.code == 409 else "outcome_unknown"
        )
        summary["recommendation"] = (
            "The deployed service has no supported proposal route. Wait for service delivery; do not switch identity or transport."
            if exc.code == 404
            else "This source event conflicts with an existing request. Restore the original envelope, or use a fresh source event only for an intentional new proposal."
            if exc.code == 409
            else "Retain and re-run the identical private envelope and source event. A service or gateway error does not prove that the plan was not saved."
        )
    message = (
        "Launchplane read was rejected; inspect the trace in an approved operator surface."
        if operation in READ_ONLY_OPERATIONS
        else "Launchplane write action was rejected; inspect the trace in an approved operator surface."
    )
    payload["warnings"] = [warning(str(summary["error_code"]), message)]
    return payload


def emit_http_error_payload(
    *, operation: str, request: dict[str, object], exc: urllib.error.HTTPError
) -> None:
    try:
        emit(summarize_http_error(operation=operation, request=request, exc=exc))
    except LaunchplaneSafetyError:
        emit_invalid_response(operation=operation, request=request)


def emit_safety_error_payload(
    *, operation: str, request: dict[str, object], exc: LaunchplaneSafetyError,
    provider_payload: object = None,
) -> None:
    unsafe_redirect = exc.code == "unsafe_redirect"
    payload = unavailable_payload(
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
    summary = cast(dict[str, object], payload["summary"])
    summary.update(service_response_diagnostics(provider_payload))
    if not unsafe_redirect and isinstance(provider_payload, (dict, ServiceResponseError)):
        summary["projection_error_code"] = exc.code
    emit(payload)


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


def emit_read_transport_error(
    *, operation: str, request: dict[str, object], timeout: float, exc: OSError | http.client.HTTPException
) -> None:
    if not (
        isinstance(exc, TimeoutError)
        or (isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError))
    ):
        payload = unavailable_payload(
            operation=operation, request=request, status="unavailable",
            code="provider_unavailable", message="Launchplane service is unavailable.",
        )
        cast(dict[str, object], payload["summary"]).update(service_response_diagnostics(exc))
        emit(payload)
        return
    payload = unavailable_payload(
        operation=operation, request=request, status="unavailable",
        code="client_timeout", message=f"Read request timed out after {timeout:g} s.",
    )
    payload["summary"] = {
        "error_code": "client_timeout", "timeout_seconds": timeout,
        "recommendation": (
            "The client read budget expired; this does not establish a service outage. "
            "No response trace was received. Retry the read with a longer --timeout before the subcommand."
        ),
    }
    cast(dict[str, object], payload["summary"]).update(service_response_diagnostics(exc))
    emit(payload)


def emit_invalid_response(
    *, operation: str, request: dict[str, object], diagnostics: dict[str, object] | None = None,
) -> None:
    payload = unavailable_payload(
        operation=operation,
        request=request,
        status="invalid",
        code="invalid_response",
        message="Launchplane returned an invalid response.",
    )
    cast(dict[str, object], payload["summary"]).update(diagnostics or {})
    assert_public_safe_shape(payload["summary"])
    emit(payload)


def read_payload_file(path: str) -> dict[str, object]:
    if path == "-":
        raise ValueError("stdin_payload_unsupported")
    payload_path = Path(path).expanduser()
    try:
        absolute_payload_path = path_with_resolved_parent(payload_path)
        resolved_payload_path = payload_path.resolve(strict=True)
        repo_root = active_repo_root()
        repo_local = _is_relative_to(absolute_payload_path, repo_root) or _is_relative_to(
            resolved_payload_path, repo_root
        )
    except OSError:
        raise ValueError("invalid_payload_path") from None
    if repo_local:
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
    expected_request_sha256: str | None = None,
) -> str:
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
    action = result.get("proposed_action")
    outcome = result.get("provider_outcome")
    retry_safe = result.get("retry_safe")
    eligible = {("adopt_observed", "present", True), ("retry_original_operation", "absent", True)}
    if expected_instance == "testing":
        eligible.add(("settle_testing_failure", "present", False))
    if expected_request_sha256 is not None:
        eligible.update({("adopt_observed", "present", False), ("close_out_observed", "absent", False)})
        if expected_instance == "testing":
            eligible.add(("abandon_testing_original", "unknown", False))
    actionable = (
        isinstance(action, str) and isinstance(outcome, str) and isinstance(retry_safe, bool)
        and (action, outcome, retry_safe) in eligible
    )
    if (
        evidence.get("operation") != "generic-web-deploy-recovery-dry-run"
        or evidence.get("status") != "ok"
        or result.get("status") != "ok"
        or result.get("mode") != "dry-run"
        or not actionable
        or result.get("recovery_digest") != expected_recovery_digest
        or result.get("product") != expected_product
        or result.get("instance") != expected_instance
    ):
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    if expected_request_sha256 is not None:
        request = evidence.get("request")
        if not isinstance(request, dict) or request.get("recovery_request_sha256") != expected_request_sha256:
            raise ValueError("reviewed_dry_run_not_apply_eligible")
    return cast(str, action)



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


def _require_apply_eligible_odoo_website_settings_dry_run(
    args: argparse.Namespace,
    *,
    expected_plan_digest: str,
    expected_product: str,
    expected_context: str,
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
    # The website result has no status field; the accepted envelope is the success signal.
    if (
        evidence.get("operation") != "odoo-website-settings-dry-run"
        or evidence.get("status") != "accepted"
        or result.get("mode") != "dry-run"
        or result.get("plan_sha256") != expected_plan_digest
        or result.get("product") != expected_product
        or result.get("context") != expected_context
        or result.get("instance") != "prod"
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
    _require_read_fields(operation, provider_payload)
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
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as exc:
        emit_read_transport_error(
            operation=operation, request=request, timeout=args.timeout, exc=exc,
        )
        return 1


def _summarize_product_read(
    *,
    operation: str,
    request: dict[str, object],
    provider_payload: dict[str, Any],
    result_key: str,
    project: Any,
    recommendation: str | None = None,
) -> dict[str, object]:
    _require_read_fields(operation, provider_payload)
    status = public_code(provider_payload.get("status"), default="ok")
    payload = base_payload(status=status, operation=operation, request=request)
    payload["result"] = project(provider_payload.get(result_key))
    summary: dict[str, object] = {
        "launchplane_status": status,
        "trace_id": public_trace_id(provider_payload.get("trace_id")),
    }
    if recommendation is not None:
        summary["recommendation"] = recommendation
    assert_public_safe_shape(summary)
    payload["summary"] = summary
    return payload


# Launchplane's operation error codes: up to four dot-separated snake_case words.
PATH_CHECK_DOTTED_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63}){1,3}$")
PATH_CHECK_DOTTED_CODE_MAX_LENGTH = 96


def _path_check_step_code(value: object) -> str:
    if (isinstance(value, str) and len(value) <= PATH_CHECK_DOTTED_CODE_MAX_LENGTH
            and PATH_CHECK_DOTTED_CODE_RE.fullmatch(value)):
        return value
    return public_code(value)


def _project_path_check(value: object) -> dict[str, object]:
    source = _require_dict(value)
    fields = {"product", "path", "state", "blocked_count", "unknown_count", "steps"}
    step_fields = {"step_id", "state", "code", "description", "fix", "record_ids"}
    states = ("clear", "blocked", "unknown")
    fixes = ("none", "code", "grant", "owner_approval", "client_acceptance", "by_hand", "wait")
    if fields.difference(source) or set(source).difference(fields | {"intent"}):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    if "intent" in source and source["intent"] not in ("provision", "generation", "testing-replacement"):
        raise LaunchplaneSafetyError("invalid_response")
    # Identifier fields have their own validator; keep all other fields strict.
    assert_public_safe_shape({key: item for key, item in source.items() if key not in {"product", "steps"}})
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
        assert_public_safe_shape({key: item for key, item in step.items() if key not in {"step_id", "record_ids"}})
        if step["state"] not in states or step["fix"] not in fixes:
            raise LaunchplaneSafetyError("invalid_response")
        record_ids = step["record_ids"]
        if not isinstance(record_ids, list) or len(record_ids) > 50:
            raise LaunchplaneSafetyError("invalid_response")
        projected_steps.append(
            {
                "step_id": public_identifier(step["step_id"]),
                "state": step["state"],
                # A failed testing deploy carries the operation's dotted error code.
                "code": _path_check_step_code(step["code"]),
                "description": public_service_text(step["description"]),
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
    projected = {
        "product": public_identifier(source["product"]),
        "path": source["path"],
        "state": state,
        "blocked_count": blocked,
        "unknown_count": unknown,
        "steps": projected_steps,
    }
    if "intent" in source:
        projected["intent"] = source["intent"]
    return projected


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
    if (result["product"] != request["product"] or result["path"] != request["path"]
            or result.get("intent") != request.get("intent")):
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
            "Compare deployment identity and fresh health evidence with the expected build. "
            "Inspect open incidents, topology warning codes, TLS states and other_warning_count. "
            "Route diagnostics do not set the sidebar color. This is attention evidence, not a green verdict; "
            "verify green with complete canonical sidebar data. Missing or truncated evidence is unknown."
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



def summarize_protected_artifacts_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    return _summarize_product_read(
        operation="protected-artifacts-read",
        request=request,
        provider_payload=provider_payload,
        result_key="protected_artifacts",
        project=_project_protected_artifacts,
        recommendation=(
            "This bounded projection explains artifact protection; truncated results "
            "are not a complete retention set for registry cleanup."
        ),
    )

def summarize_preview_history_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    _require_read_fields("preview-history-read", provider_payload)
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
    _require_read_fields("reconcile-requests-read", provider_payload)
    status = public_code(provider_payload.get("status"), default="ok")
    payload = base_payload(status=status, operation="reconcile-requests-read", request=request)
    target_key = request.get("target_key")
    payload["result"] = _project_reconcile_requests(
        provider_payload, target_key=public_identifier(target_key) if target_key is not None else None
    )
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


def _recovery_reference(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"event-deploy-[0-9a-f]{64}", value) is None:
        raise LaunchplaneSafetyError("invalid_recovery_reference")
    return value


def summarize_recovery_reference_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    _require_read_fields(
        "generic-web-deploy-recovery-reference-read",
        provider_payload,
        required=BOUNDED_READ_RESPONSE_FIELDS["generic-web-deploy-recovery-reference-read"],
    )
    if (
        provider_payload["product"] != request["product"]
        or provider_payload["instance"] != "testing"
        or not isinstance(provider_payload["reservation_state"], str)
        or provider_payload["reservation_state"] not in {"running", "reconcile_required"}
        or type(provider_payload["reservation_attempt"]) is not int
        or provider_payload["reservation_attempt"] < 1
    ):
        raise LaunchplaneSafetyError("invalid_response")
    payload = base_payload(status="ok", operation="generic-web-deploy-recovery-reference-read", request=request)
    payload["result"] = {
        "product": public_identifier(provider_payload["product"]),
        "context": public_identifier(provider_payload["context"]),
        "instance": "testing",
        "recovery_reference": _recovery_reference(provider_payload["recovery_reference"]),
        "reservation_state": provider_payload["reservation_state"],
        "reservation_attempt": provider_payload["reservation_attempt"],
    }
    payload["summary"] = {"recommendation": "Dry-run this exact service-owned reference; unknown provider outcomes remain held."}
    assert_public_safe_shape(payload["result"])
    return payload


def summarize_product_secret_bindings_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    _require_read_fields("product-secret-bindings-read", provider_payload)
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


def _project_target_replacement_recovery(value: object) -> dict[str, object] | None:
    """Validate the service's TestingReplacementRecoveryRead, including exclude_none serialization."""
    if value is None:
        return None
    source = _require_dict(value)
    nullable = {"attempt_matches", "binding_matches", "effects_acknowledged"}
    codes = {
        "status": {"refused", "eligible"},
        "reason": {
            "", "recovery_scope_unsupported", "recovery_operation_not_held",
            "recovery_execution_exclusion_unavailable", "recovery_operation_changed",
            "recovery_authorization_changed", "recovery_journal_absent",
            "recovery_attempt_changed", "recovery_binding_changed",
            "recovery_dispatch_unacknowledged", "recovery_effect_unsupported",
            "recovery_inspection_before_fence", "recovery_provider_evidence_incomplete",
            "recovery_operation_or_binding_changed", "recovery_evidence_unavailable",
        },
        "journal": {"present", "absent", "unknown"},
        "provider_effects": {"unknown", "quiescent_observed"},
    }
    required = {*codes, "effect_count", "provider_inspected", "safe_to_release"}
    if not required <= source.keys() or source.keys() - required - nullable:
        raise LaunchplaneSafetyError("unsafe_response_shape")
    for name, allowed in codes.items():
        if not isinstance(source[name], str) or source[name] not in allowed:
            raise LaunchplaneSafetyError("invalid_response")
    for name in nullable:
        if source.get(name) is not None and type(source[name]) is not bool:
            raise LaunchplaneSafetyError("invalid_response")
    count = source["effect_count"]
    if type(count) is not int or count < 0:
        raise LaunchplaneSafetyError("invalid_response")
    if type(source["provider_inspected"]) is not bool or source["safe_to_release"] is not False:
        raise LaunchplaneSafetyError("invalid_response")
    projected = {**source, **{name: source.get(name) for name in nullable}}
    assert_public_safe_shape(projected)
    return projected


def summarize_target_replacement_operation_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    _require_read_fields("target-replacement-operation-read", provider_payload)
    status = public_code(provider_payload.get("status"), default="ok")
    payload = base_payload(
        status=status, operation="target-replacement-operation-read", request=request
    )
    result = _project_target_replacement_operation(provider_payload)
    if "recovery" in provider_payload:
        result["recovery"] = _project_target_replacement_recovery(provider_payload["recovery"])
    payload["result"] = result
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
            "key-list blocker is about, and expected_artifact_id is the artifact the plan "
            "evaluated. Blocker, step and warning text is not returned."
        ),
    }
    assert_public_safe_shape(payload["summary"])
    return payload


def _target_replacement_plan_candidate(args: argparse.Namespace) -> dict[str, str]:
    """The candidate artifact to plan with instead of the lane's current one: both or neither."""
    artifact_id = (getattr(args, "artifact_id", None) or "").strip()
    source_git_ref = (getattr(args, "source_git_ref", None) or "").strip()
    if bool(artifact_id) != bool(source_git_ref):
        raise ValueError("artifact_id_and_source_git_ref_required_together")
    if not artifact_id:
        return {}
    candidate: dict[str, str] = {}
    for name, value in (("artifact_id", artifact_id), ("source_git_ref", source_git_ref)):
        try:
            candidate[name] = public_identifier(value)
        except LaunchplaneSafetyError:
            raise ValueError(f"invalid_{name}") from None
    return candidate


def execute_target_replacement_plan_read(*, args: argparse.Namespace) -> int:
    """POST the read-only plan route: it builds a plan and writes no record."""
    product = public_identifier(_required_argument(args, "product"))
    instance = public_identifier(_required_argument(args, "instance"))
    for name, value in (("product", product), ("instance", instance)):
        if not PRODUCT_READ_PATH_SEGMENT_RE.fullmatch(value):
            raise ValueError(f"invalid_{name}")
    candidate = _target_replacement_plan_candidate(args)
    operation = "target-replacement-plan-read"
    request: dict[str, object] = {
        "product": product,
        "instance": instance,
        **candidate,
        "payload_source": "operator_argument",
    }
    body: dict[str, object] = {
        "schema_version": 1,
        "product": product,
        "replacement": {"product": product, "instance": instance, **candidate},
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
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as exc:
        emit_read_transport_error(
            operation=operation, request=request, timeout=args.timeout, exc=exc,
        )
        return 1


def summarize_merge_train_policy_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    """Project one repository; never emit credential sources or the fleet."""
    _require_read_fields(
        "merge-train-policy-read", provider_payload, required=frozenset({"status", "trace_id", "record"})
    )
    record = _require_dict(provider_payload.get("record"))
    policy = _require_dict(record.get("policy"))
    policies = policy.get("policies")
    if provider_payload.get("status") != "ok" or record.get("status") != "active" or not isinstance(policies, list) or not policies:
        raise LaunchplaneSafetyError("invalid_response")
    repository = public_identifier(request.get("repository"))
    targets: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in policies:
        target = _require_dict(value)
        name = target.get("repository")
        branch = target.get("base_branch")
        if not isinstance(name, str) or not name.strip() or not isinstance(branch, str) or not branch.strip():
            raise LaunchplaneSafetyError("invalid_response")
        key = (name.casefold(), branch)
        if key in seen:
            raise LaunchplaneSafetyError("invalid_response")
        seen.add(key)
        if name.casefold() == repository.casefold():
            raw_label = target.get("enqueue_label")
            public_summary_string(raw_label, max_length=50)
            if not isinstance(raw_label, str) or len(raw_label) > 50 or raw_label != raw_label.strip():
                raise LaunchplaneSafetyError("invalid_response")
            if any(ord(character) < 32 or ord(character) == 127 for character in raw_label):
                raise LaunchplaneSafetyError("invalid_response")
            targets.append({
                "baseBranch": public_identifier(branch),
                "readyLabel": raw_label,
            })
    summary = {
        "source": "launchplane",
        "status": "enrolled" if targets else "not_enrolled",
        "enabled": bool(targets),
        "targets": sorted(targets, key=lambda entry: entry["baseBranch"]),
        "policy": {
            "record_id": public_identifier(record.get("record_id")),
            "updated_at": public_timestamp(record.get("updated_at")),
            "policy_sha256": _project_sha256(record.get("policy_sha256")),
        },
        "trace_id": public_trace_id(provider_payload.get("trace_id")),
    }
    assert_public_safe_shape(summary)
    payload = base_payload(status="available", operation="merge-train-policy-read", request=request)
    payload["result"] = summary
    return payload


def summarize_governance_projection_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    operation = "governance-projection-read"
    _require_read_fields(operation, provider_payload, required=BOUNDED_READ_RESPONSE_FIELDS[operation])
    if provider_payload["status"] != "ok":
        raise LaunchplaneSafetyError("invalid_response")
    source = _require_dict(provider_payload["projection"])
    fields = {"schema_version", "mode", "authoritative", "authorizes", "target", "owner_judgment",
              "merge_readiness", "merge_admission", "landing_outcome", "advisory_observations", "generated_at"}
    _require_exact_fields(source, fields | ({"requested_target"} if "requested_target" in source else set()))
    if source["schema_version"] != 1 or source["mode"] != "read_only_projection" or source["authoritative"] is not False or source["authorizes"] != []:
        raise LaunchplaneSafetyError("invalid_response")
    target = _require_dict(source["target"] if source["target"] is not None else source.get("requested_target"))
    if str(target.get("repository", "")).casefold() != str(request["repository"]).casefold() or target.get("pull_request_number") != request["pull_request_number"]:
        raise LaunchplaneSafetyError("invalid_response")
    readiness = _require_dict(source["merge_readiness"])
    readiness_fields = {"level", "mode", "authoritative", "authorizes", "availability", "reason_code", "result"}
    _require_exact_fields(readiness, readiness_fields | ({"detail"} if "detail" in readiness else set()))
    availability = readiness["availability"]
    result = readiness["result"]
    if availability not in {"available", "unavailable", "not_active"} or (availability == "available") != isinstance(result, dict):
        raise LaunchplaneSafetyError("invalid_response")
    if readiness["authoritative"] is not False or readiness["authorizes"] != []:
        raise LaunchplaneSafetyError("invalid_response")
    if source["target"] is None and availability != "unavailable":
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, Any] = {
        "authoritative": False, "authorizes": [], "current_target_available": source["target"] is not None,
        "generated_at": public_timestamp(source["generated_at"]),
        "merge_readiness": {
            "availability": public_code(availability), "reason_code": public_code(readiness["reason_code"]),
            "state": public_code(result.get("state")) if isinstance(result, dict) else None,
        },
    }
    for name, allowed_fields, id_field, empty_status in (
        ("merge_admission", {"level", "mode", "authoritative", "current_effect_authority", "authorizes", "status", "record"}, "admission_id", "not_recorded"),
        ("landing_outcome", {"mode", "authoritative", "authorizes", "target_status", "status", "landed", "record"}, "outcome_id", "not_observed"),
    ):
        facet = _require_exact_fields(source[name], allowed_fields)
        if facet["authoritative"] is not False:
            raise LaunchplaneSafetyError("invalid_response")
        if name == "merge_admission" and facet["current_effect_authority"] is not False:
            raise LaunchplaneSafetyError("invalid_response")
        record = facet["record"]
        if (record is None) != (facet["status"] == empty_status):
            raise LaunchplaneSafetyError("invalid_response")
        summary: dict[str, Any] = {"status": public_code(facet["status"]), "record": None}
        if name == "landing_outcome":
            summary["target_status"] = public_code(facet["target_status"])
            if facet["landed"] is not (facet["status"] == "landed"):
                raise LaunchplaneSafetyError("invalid_response")
        if record is not None:
            record = _require_dict(record)
            summary["record"] = {id_field: public_identifier(record.get(id_field))}
            if name == "landing_outcome":
                summary["record"]["status"] = public_code(record.get("status"))
                if record.get("status") != facet["status"]:
                    raise LaunchplaneSafetyError("invalid_response")
                merge_commit = record.get("merge_commit_sha")
                summary["record"]["merge_commit_sha"] = _public_commit_sha(merge_commit) if merge_commit else None
            else:
                summary["record"]["decision"] = public_code(record.get("decision"))
        projected[name] = summary
    assert_public_safe_shape(projected)
    payload = base_payload(status="ok", operation=operation, request=request)
    payload["result"] = projected
    payload["summary"] = {"trace_id": public_trace_id(provider_payload["trace_id"])}
    return payload


PRODUCT_READ_SUMMARIZERS = {
    "governance-projection-read": summarize_governance_projection_read,
    "generic-web-deploy-recovery-reference-read": summarize_recovery_reference_read,
    "merge-train-policy-read": summarize_merge_train_policy_read,
    "path-check": summarize_path_check,
    "product-environment-read": summarize_product_environment_read,
    "product-activity-read": summarize_product_activity_read,
    "protected-artifacts-read": summarize_protected_artifacts_read,
    "product-profile-read": summarize_product_profile_read,
    "preview-history-read": summarize_preview_history_read,
    "reconcile-requests-read": summarize_reconcile_requests_read,
    "product-secret-bindings-read": summarize_product_secret_bindings_read,
    "target-replacement-operation-read": summarize_target_replacement_operation_read,
}


def summarize_names_only_read(
    *, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    """Project only named metadata fields; never inspect settings values or audit data."""
    operation = str(request["operation"])
    field = NAMES_ONLY_READS[operation]
    if (
        provider_payload.get("status") != "ok"
        or provider_payload.get("context") != request["context"]
        or provider_payload.get("instance") != request["instance"]
    ):
        raise LaunchplaneSafetyError("invalid_response")
    records = provider_payload.get(field)
    if not isinstance(records, list):
        raise LaunchplaneSafetyError("invalid_response")
    projected = []
    names_truncated = False
    # The service accepts any non-empty key, so an unusual name is counted, not fatal;
    # a secret-looking one still fails the read.
    drops = _FieldDrops()
    allowed_scopes = ("global", "context", "instance") if field == "settings" else (
        "global", "context", "context_instance"
    )
    for raw in records[:NAMES_ONLY_MAX_ITEMS]:
        record = _require_dict(raw)
        scope = record.get("scope")
        if scope not in allowed_scopes:
            raise LaunchplaneSafetyError("invalid_response")
        item: dict[str, object] = {"scope": scope}
        if field == "settings":
            for name in ("context", "instance"):
                value = record.get(name)
                item[name] = "" if value == "" else public_identifier(value)
            for name in ("keys", "withheld_keys"):
                values = record.get(name)
                if not isinstance(values, list):
                    raise LaunchplaneSafetyError("invalid_response")
                names_truncated |= len(values) > NAMES_ONLY_MAX_ITEMS
                kept = [drops.keep(f"settings[].{name}[]", _public_env_key, value)
                        for value in values[:NAMES_ONLY_MAX_ITEMS]]
                item[name] = [value for value in kept if value]
        else:
            for name in ("name", "integration"):
                item[name] = public_identifier(record.get(name))
            item["status"] = public_code(record.get("status"))
        projected.append(item)
    result = {field: projected, "records_truncated": len(records) > NAMES_ONLY_MAX_ITEMS,
              "names_truncated": names_truncated, "dropped_name_count": drops.count}
    assert_public_safe_shape(result)
    payload = base_payload(status="ok", operation=operation, request=request)
    payload["result"] = result
    payload["summary"] = {
        "trace_id": public_trace_id(provider_payload.get("trace_id")),
        "recommendation": "Names and metadata only; lists reflect caller access, not proof of absence or effective runtime values.",
    }
    return payload


PRODUCT_READ_SUMMARIZERS.update({command: summarize_names_only_read for command in NAMES_ONLY_READS})


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
    provider_payload = None
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
        emit_safety_error_payload(
            operation=operation, request=request, exc=exc, provider_payload=provider_payload
        )
        return 1
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as exc:
        emit_read_transport_error(
            operation=operation, request=request, timeout=args.timeout, exc=exc,
        )
        return 1


def _load_reviewed_evidence(
    args: argparse.Namespace,
    *,
    operation: str,
    expected_digest: str,
    digest_field: str = "plan_sha256",
    digest_result_key: str | None = None,
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
    digest_result = result.get(digest_result_key) if digest_result_key and isinstance(result, dict) else result
    if (
        not isinstance(result, dict)
        or not isinstance(evidence.get("request"), dict)
        or evidence.get("operation") != operation
        or evidence.get("status") != evidence_status
        or (result_status is not None and result.get("status") != result_status)
        or result.get("mode") != dry_run_mode
        or not isinstance(digest_result, dict)
        or digest_result.get(digest_field) != expected_digest
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


HEALTH_MONITORING_KINDS = {"public_http", "private_http"}
HEALTH_MONITORING_INTENTS = {"public", "private", "prelaunch"}
HEALTH_MONITORING_PLAN_FIELDS = {
    "status", "mode", "product", "context", "instance", "check_name", "current_check_kind",
    "requested_check_kind", "current_monitoring_intent", "requested_monitoring_intent",
    "operation", "current_enabled", "requested_enabled", "current_require_runtime_identity",
    "requested_require_runtime_identity", "private_endpoint_key", "resolved_url", "changed",
    "applied", "reason", "source_label", "profile_updated_at_before", "profile_updated_at_after",
    "profile_sha256_before", "plan_sha256", "current_dependency_health", "requested_dependency_health",
}
HEALTH_DEPENDENCY_MODES = {"informational", "required"}
HEALTH_DEPENDENCY_SOURCES = {"derived", "override"}
HEALTH_DEPENDENCIES = {"database", "email", "cache"}


def _dependency_health_policy(value: object) -> dict[str, object]:
    """Normalize a lane dependency policy; an absent policy is the service default."""
    if value is None:
        return {"mode": "informational", "override": None}
    source = _require_dict(value)
    override = source.get("override")
    if source.keys() - {"mode", "override"} or source.get("mode") not in HEALTH_DEPENDENCY_MODES:
        raise LaunchplaneSafetyError("invalid_response")
    if override is None:
        return {"mode": source["mode"], "override": None}
    override = _require_dict(override)
    dependencies = override.get("dependencies")
    reason = override.get("reason")
    if (
        override.keys() != {"dependencies", "reason"} or not isinstance(dependencies, list)
        or not set(dependencies) <= HEALTH_DEPENDENCIES or len(set(dependencies)) != len(dependencies)
        or not isinstance(reason, str) or not reason.strip()
    ):
        raise LaunchplaneSafetyError("invalid_response")
    return {"mode": source["mode"], "override": {"dependencies": sorted(dependencies), "reason": reason.strip()}}


def _public_dependency_health(policy: dict[str, object]) -> dict[str, object]:
    # Override reasons are free text; receipts keep only the mode and dependency kinds.
    override = policy["override"]
    return {"mode": policy["mode"], "override": None if override is None else {
        "dependencies": cast(dict[str, object], override)["dependencies"],
    }}


def _requested_dependency_health(args: argparse.Namespace) -> dict[str, object] | None:
    mode = getattr(args, "dependency_mode", None)
    source = getattr(args, "dependency_source", None)
    dependencies = list(getattr(args, "dependency", None) or [])
    reason = (getattr(args, "dependency_override_reason", "") or "").strip()
    if mode is None:
        if source is not None or dependencies or reason:
            raise ValueError("dependency_mode_required")
        return None
    # The service replaces the whole policy, so an override is never cleared implicitly.
    if source is None:
        raise ValueError("dependency_source_required")
    if source == "derived":
        if dependencies or reason:
            raise ValueError("derived_dependency_rejects_override")
        return {"mode": mode, "override": None}
    if not reason:
        raise ValueError("dependency_override_reason_required")
    if len(reason) > 1000:
        raise ValueError("dependency_override_reason_too_long")
    if len(set(dependencies)) != len(dependencies):
        raise ValueError("duplicate_dependency")
    return {"mode": mode, "override": {"dependencies": sorted(dependencies), "reason": reason}}


def _health_check_token(name: str) -> str:
    # The service matches check names using this normalized record token.
    return "".join(char if char.isalnum() else "-" for char in name.strip().lower()).strip("-")


def health_monitoring_body(
    args: argparse.Namespace, *, mode: str
) -> tuple[dict[str, object], dict[str, object]]:
    body: dict[str, object] = {
        "schema_version": 1, "mode": mode,
        **{field: _required_argument(args, field) for field in (
            "product", "context", "instance", "check_name", "reason",
        )},
        "check_kind": args.check_kind, "monitoring_intent": args.monitoring_intent,
        "enabled": args.enabled, "require_runtime_identity": args.require_runtime_identity,
        "private_endpoint_key": args.private_endpoint_key.strip(),
    }
    dependency_health = _requested_dependency_health(args)
    if dependency_health is not None:
        # Omission keeps the request and its saved evidence identical to legacy calls.
        body["dependency_health"] = dependency_health
    for field in ("product", "context", "instance", "check_name"):
        if not PRODUCT_READ_PATH_SEGMENT_RE.fullmatch(cast(str, body[field])):
            raise ValueError(f"invalid_{field}")
    if body["private_endpoint_key"] and not PRODUCT_READ_PATH_SEGMENT_RE.fullmatch(
        cast(str, body["private_endpoint_key"])
    ):
        raise ValueError("invalid_private_endpoint_key")
    if body["check_kind"] == "public_http" and body["private_endpoint_key"]:
        raise ValueError("public_check_rejects_private_endpoint")
    if body["check_kind"] == "private_http" and body["enabled"] and not body["private_endpoint_key"]:
        raise ValueError("private_endpoint_key_required")
    if not body["enabled"] and body["require_runtime_identity"]:
        raise ValueError("disabled_check_rejects_runtime_identity")
    request: dict[str, object] = {
        "mode": mode, "payload_source": "operator_argument",
        "payload_digest": metadata_review_digest(body),
        **{field: public_identifier(body[field]) for field in (
            "product", "context", "instance", "check_name",
        )},
        **{field: body[field] for field in (
            "check_kind", "monitoring_intent", "enabled", "require_runtime_identity",
        )},
    }
    if dependency_health is not None:
        request["dependency_health"] = _public_dependency_health(dependency_health)
    if mode == "apply":
        digest = _reviewed_apply_digest(args)
        evidence, reviewed = _load_reviewed_evidence(
            args, operation="health-monitoring-dry-run", expected_digest=digest,
        )
        if (
            evidence["request"] != {**request, "mode": "dry-run"}
            or reviewed.get("applied") is not False
            or any(reviewed.get(field) != body[field] for field in ("product", "context", "instance"))
            or _health_check_token(str(reviewed.get("check_name", ""))) != _health_check_token(args.check_name)
            or any(reviewed.get("requested_" + field) != body[field] for field in (
                "check_kind", "monitoring_intent", "enabled", "require_runtime_identity",
            ))
            or not re.fullmatch(r"[0-9a-f]{64}", str(reviewed.get("endpoint_sha256", "")))
            or (dependency_health is not None
                and reviewed.get("requested_dependency_health") != request["dependency_health"])
        ):
            raise ValueError("reviewed_dry_run_not_apply_eligible")
        body["reviewed_plan_sha256"] = digest
    return body, request


def _project_health_monitoring_plan(
    value: object, *, request: dict[str, object]
) -> dict[str, object]:
    source = _require_dict(value)
    if source.keys() - HEALTH_MONITORING_PLAN_FIELDS:
        raise LaunchplaneSafetyError("unsafe_response_shape")
    mode = _reviewed_plan_mode(source.get("mode"))
    if (
        source.get("status") != "ok" or mode != request.get("mode")
        or source.get("operation") not in {"create", "update", "unchanged"}
        or source.get("current_check_kind") not in HEALTH_MONITORING_KINDS | {None}
        or source.get("current_monitoring_intent") not in HEALTH_MONITORING_INTENTS
        or source.get("applied") is not (mode == "apply")
        or type(source.get("changed")) is not bool
        or any(type(source.get(field)) is not bool for field in (
            "requested_enabled", "requested_require_runtime_identity",
        ))
        or not isinstance(source.get("resolved_url"), str)
        or any(source.get(field) != request.get(field) for field in ("product", "context", "instance"))
        or _health_check_token(str(source.get("check_name", ""))) != _health_check_token(str(request.get("check_name", "")))
        or any(source.get("requested_" + field) != request.get(field) for field in (
            "check_kind", "monitoring_intent", "enabled", "require_runtime_identity",
        ))
    ):
        raise LaunchplaneSafetyError("invalid_response")
    dependency_fields = {"current_dependency_health", "requested_dependency_health"} & source.keys()
    if dependency_fields and len(dependency_fields) != 2:
        raise LaunchplaneSafetyError("invalid_response")
    dependency_review: dict[str, object] = {}
    if dependency_fields:
        current = _public_dependency_health(_dependency_health_policy(source["current_dependency_health"]))
        requested = _public_dependency_health(_dependency_health_policy(source["requested_dependency_health"]))
        # Without a requested policy the service must preserve the lane's current one.
        if requested != request.get("dependency_health", current):
            raise LaunchplaneSafetyError("invalid_response")
        dependency_review = {"current_dependency_health": current, "requested_dependency_health": requested}
    elif "dependency_health" in request:
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "status": "ok", "mode": mode, "operation": source["operation"],
        **{field: public_identifier(source.get(field)) for field in (
            "product", "context", "instance", "check_name",
        )},
        **{field: source[field] for field in (
            "requested_check_kind", "current_monitoring_intent", "requested_monitoring_intent",
            "requested_enabled", "requested_require_runtime_identity", "changed", "applied",
        )},
        "current_check_kind": source.get("current_check_kind"),
        "current_enabled": _optional_bool(source.get("current_enabled")),
        "current_require_runtime_identity": _optional_bool(source.get("current_require_runtime_identity")),
        "endpoint_sha256": _canonical_sha256(source["resolved_url"]),
        "plan_sha256": _required_lower_sha256(source.get("plan_sha256"), code="invalid_response"),
        **dependency_review,
    }
    assert_public_safe_shape(projected)
    return projected


def read_health_monitoring(
    *, settings: dict[str, str], body: dict[str, object], timeout: float
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    provider = request_launchplane_read(
        service_url=settings["service_url"], settings=settings, query={}, timeout=timeout,
        path=_product_read_path("product-profile-read", product=cast(str, body["product"])),
    )
    try:
        profile = _require_dict(provider.get("profile"))
        if provider.get("status") != "ok" or profile.get("product") != body["product"]:
            raise LaunchplaneSafetyError("invalid_response")
        lanes = profile.get("lanes")
        if not isinstance(lanes, list):
            raise LaunchplaneSafetyError("invalid_response")
        matches = [lane for lane in lanes if isinstance(lane, dict) and all(
            lane.get(field) == body[field] for field in ("context", "instance")
        )]
        if len(matches) != 1:
            raise LaunchplaneSafetyError("invalid_response")
        lane = matches[0]
        policy = _require_dict(lane.get("health_monitoring"))
        checks = policy.get("checks")
        if not isinstance(checks, list):
            raise LaunchplaneSafetyError("invalid_response")
        matches = [check for check in checks if isinstance(check, dict) and
                   _health_check_token(str(check.get("name", ""))) == _health_check_token(cast(str, body["check_name"]))]
        if len(matches) > 1:
            raise LaunchplaneSafetyError("invalid_response")
        check = matches[0] if matches else {}
        endpoint = {"url": check.get("url", ""), "health_url": lane.get("health_url", ""),
                    "base_url": lane.get("base_url", ""), "private_endpoint_key": check.get("private_endpoint_key", "")}
        if not all(isinstance(value, str) for value in endpoint.values()):
            raise LaunchplaneSafetyError("invalid_response")
        public = {
            "monitoring_intent": policy.get("monitoring_intent"), "kind": check.get("kind"),
            "enabled": check.get("enabled"), "require_runtime_identity": check.get("require_runtime_identity"),
        }
        if (
            public["monitoring_intent"] not in HEALTH_MONITORING_INTENTS
            or (check and (public["kind"] not in HEALTH_MONITORING_KINDS
                          or type(public["enabled"]) is not bool
                          or type(public["require_runtime_identity"]) is not bool))
        ):
            raise LaunchplaneSafetyError("invalid_response")
        dependency_health = _dependency_health_policy(policy.get("dependency_health"))
        public["dependency_health"] = _public_dependency_health(dependency_health)
        assert_public_safe_shape(public)
        return public, endpoint, dependency_health
    except (LaunchplaneSafetyError, ValueError, TypeError) as exc:
        raise ServiceResponseError(service_response_diagnostics(provider)) from exc


def execute_health_monitoring_apply(
    *, args: argparse.Namespace, body: dict[str, object], request: dict[str, object]
) -> int:
    _, reviewed = _load_reviewed_evidence(
        args, operation="health-monitoring-dry-run", expected_digest=cast(str, body["reviewed_plan_sha256"]),
    )
    before: dict[str, object] = {}
    before_dependency_health: dict[str, object] = {}

    def preflight(settings: dict[str, str]) -> dict[str, object] | None:
        _policy, endpoint, dependency_health = read_health_monitoring(settings=settings, body=body, timeout=args.timeout)
        resolved_url = (endpoint["url"] or endpoint["health_url"] or endpoint["base_url"]) if body["check_kind"] == "public_http" else ""
        if _canonical_sha256(resolved_url) != reviewed["endpoint_sha256"]:
            return {"error_code": "health_endpoint_changed_since_review",
                    "recommendation": "The endpoint changed since review; run a new dry-run."}
        before.update(endpoint)
        before_dependency_health.update(dependency_health)
        return None

    def finish(settings: dict[str, str], _provider: dict[str, Any], payload: dict[str, Any]) -> bool:
        result = payload["result"]
        applied_as_reviewed = (result["plan_sha256"] == body["reviewed_plan_sha256"]
                               and result["endpoint_sha256"] == reviewed["endpoint_sha256"])
        if not applied_as_reviewed:
            payload["warnings"].append(warning("applied_plan_differs_from_review", "The applied monitoring plan differs from review."))

        def read() -> dict[str, object]:
            policy, endpoint, dependency_health = read_health_monitoring(settings=settings, body=body, timeout=args.timeout)
            # A new private check may adopt its reviewed registered key. Public URLs,
            # including an empty stored URL with lane fallback, stay byte-for-byte unchanged.
            expected_endpoint = {**before}
            if body["check_kind"] == "private_http" and body["private_endpoint_key"]:
                expected_endpoint["private_endpoint_key"] = body["private_endpoint_key"]
            # An omitted dependency policy must survive the edit unchanged, reason included.
            expected_dependency_health = body.get("dependency_health", before_dependency_health)
            return {**policy, "endpoint_preserved": endpoint == expected_endpoint,
                    "dependency_health_matches": dependency_health == expected_dependency_health}

        verified = attach_read_back(
            payload, read=read, label="health monitoring policy",
            matches=lambda observed: observed["endpoint_preserved"] is True
            and observed["dependency_health_matches"] is True and all(
                observed[field] == body[target] for field, target in (
                    ("monitoring_intent", "monitoring_intent"), ("kind", "check_kind"),
                    ("enabled", "enabled"), ("require_runtime_identity", "require_runtime_identity"),
                )
            ),
        )
        return applied_as_reviewed and verified

    return execute_verified_apply(
        args=args, operation="health-monitoring-apply", request=request, body=body,
        path=helper_command_path("health-monitoring-apply"), preflight=preflight,
        finish=finish, label="health monitoring policy",
    )


def testing_hold_body(args: argparse.Namespace, *, mode: str) -> dict[str, object]:
    purpose = _testing_hold_purpose(getattr(args, "purpose", "staff_testing"))
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
    # Preserve the raw legacy staff payload so an uncertain apply can replay its original key.
    if purpose != "staff_testing":
        body["purpose"] = purpose
    if mode == "apply":
        expected_plan_digest = _reviewed_apply_digest(args)
        evidence, result = _load_reviewed_evidence(
            args, operation="testing-hold-dry-run", expected_digest=expected_plan_digest
        )
        # A no-op lift has no hold snapshot; bind its purpose from the saved request too.
        reviewed_purpose = _testing_hold_purpose(evidence["request"].get("purpose", "staff_testing"))
        reviewed_hold = _project_testing_hold(result.get("after") if body["hold"] else result.get("before"))
        if (
            result.get("product") != body["product"]
            or result.get("context") != body["context"]
            or result.get("instance") != body["instance"]
            # A lift's reason is not in the plan digest, so bind direction and reason here.
            or (result.get("after") is not None) != body["hold"]
            or public_operator_text(result.get("reason")) != public_operator_text(body["reason"])
            or reviewed_purpose != purpose
            or (
                reviewed_hold is not None
                and reviewed_hold.get("purpose", "staff_testing") != purpose
            )
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


def service_runtime_setting_body(args: argparse.Namespace, *, mode: str) -> dict[str, object]:
    key = _required_argument(args, "key")
    if SERVICE_RUNTIME_SETTING_KEY_RE.fullmatch(key) is None:
        raise ValueError("invalid_key")
    body: dict[str, object] = {
        "mode": mode,
        "key": key,
        "value": args.value,
        "reason": _required_argument(args, "reason"),
    }
    if mode == "apply":
        expected_plan_digest = _reviewed_apply_digest(args)
        _evidence, result = _load_reviewed_evidence(
            args,
            operation="service-runtime-setting-dry-run",
            expected_digest=expected_plan_digest,
            digest_field="plan_digest",
        )
        if (
            result.get("key") != key
            or result.get("value_sha256") != _service_runtime_setting_value_sha256(args.value)
        ):
            raise ValueError("reviewed_dry_run_not_apply_eligible")
        body["expected_plan_digest"] = expected_plan_digest
    return body


def _service_runtime_setting_value_sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


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


def _odoo_website_settings_intent(value: object) -> dict[str, object] | None:
    # Null withdraws the pending intent. The service owns GA4 and consent rules.
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("website_settings_required")
    if any(str(key) not in ODOO_WEBSITE_SETTINGS_INTENT_FIELDS for key in value):
        raise ValueError("unsupported_website_settings_field")
    artifact_id = value.get("artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise ValueError("artifact_id_required")
    settings = value.get("settings")
    if not isinstance(settings, dict):
        raise ValueError("website_settings_required")
    if any(str(key) not in ODOO_WEBSITE_SETTINGS_FIELDS for key in settings):
        raise ValueError("unsupported_website_settings_field")
    if settings.get("operation", "apply") != "apply":
        raise ValueError("unsupported_website_settings_operation")
    website_id = settings.get("website_id")
    if isinstance(website_id, bool) or not isinstance(website_id, int) or website_id < 1:
        raise ValueError("website_id_required")
    origin = settings.get("public_origin")
    try:
        parts = urllib.parse.urlsplit(origin) if isinstance(origin, str) else None
    except ValueError:
        parts = None
    if parts is None or parts.scheme != "https" or not parts.hostname:
        raise ValueError("https_public_origin_required")
    for key in ("public_indexing_enabled", "cookies_bar", "block_third_party_domains"):
        if key in settings and not isinstance(settings[key], bool):
            raise ValueError(f"{key}_boolean_required")
    if not isinstance(settings.get("google_analytics_key", ""), str):
        raise ValueError("google_analytics_key_string_required")
    return {"artifact_id": artifact_id.strip(), "settings": settings}


def odoo_website_settings_body(args: argparse.Namespace, *, mode: str) -> dict[str, object]:
    payload = read_payload_file(args.payload_file)
    if any(str(key) not in ODOO_WEBSITE_SETTINGS_PAYLOAD_FIELDS for key in payload):
        raise ValueError("unsupported_payload_field")
    body: dict[str, object] = {}
    for field in ("product", "context", "reason"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field}_required")
        body[field] = value.strip()
    if payload.get("instance", "prod") != "prod":
        raise ValueError("unsupported_instance")
    body["instance"] = "prod"
    if "website_settings" not in payload:
        raise ValueError("website_settings_required")
    body["website_settings"] = _odoo_website_settings_intent(payload["website_settings"])
    body["mode"] = mode
    if mode == "apply":
        _require_idempotency(args)
        if not args.reviewed_dry_run:
            raise ValueError("reviewed_dry_run_required")
        expected_plan_digest = args.expected_plan_digest.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_plan_digest):
            raise ValueError("invalid_expected_plan_digest")
        _require_apply_eligible_odoo_website_settings_dry_run(
            args,
            expected_plan_digest=expected_plan_digest,
            expected_product=cast(str, body["product"]),
            expected_context=cast(str, body["context"]),
        )
        body["reviewed_plan_sha256"] = expected_plan_digest
    return body


def _recovery_request_sha256(body: dict[str, object]) -> str:
    reviewed = {key: value for key, value in body.items() if key != "expected_recovery_digest"}
    return hashlib.sha256(json.dumps(reviewed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _recovery_request(body: dict[str, object], *, mode: str) -> dict[str, object]:
    request: dict[str, object] = {"mode": mode, "payload_source": "private_file"}
    if "recovery_reference" in body:
        request.update({
            "product": body["product"], "instance": body["instance"],
            "recovery_request_sha256": _recovery_request_sha256(body),
        })
    return request


def generic_web_deploy_recovery_body(
    args: argparse.Namespace, *, mode: str
) -> dict[str, object]:
    body = read_payload_file(args.payload_file)
    reference = "recovery_reference" in body
    if reference:
        allowed = {"product", "instance", "recovery_reference", "reason"}
        if mode == "apply":
            allowed.add("expected_recovery_digest")
        if set(body) - allowed:
            raise ValueError("invalid_recovery_reference_payload")
        _recovery_reference(body["recovery_reference"])
        if args.idempotency_key:
            raise ValueError("reference_uses_service_owned_idempotency")
        if body.get("instance") != "testing":
            raise ValueError("recovery_reference_testing_only")
    else:
        _require_idempotency(args)
    if not reference and body.get("schema_version") != 1:
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
    if reference and len(reason) > 1000:
        raise ValueError("invalid_recovery_reason")
    if not reference and not isinstance(original_deploy, dict):
        raise ValueError("original_deploy_required")
    if not reference:
        assert isinstance(original_deploy, dict)
        deploy_request = original_deploy.get("deploy")
        if (
            str(original_deploy.get("product") or "").strip() != product.strip()
            or not isinstance(deploy_request, dict)
            or str(deploy_request.get("instance") or "").strip() != instance.strip()
        ):
            raise ValueError("original_deploy_identity_mismatch")
    else:
        body["product"] = public_identifier(product)
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
        args.reviewed_recovery_action = _require_apply_eligible_recovery_dry_run(
            args,
            expected_recovery_digest=expected_recovery_digest,
            expected_product=product.strip(),
            expected_instance=instance.strip(),
            expected_request_sha256=_recovery_request_sha256(body) if reference else None,
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
    if "odoo_business_hours" in body:
        body.setdefault("source_label", "product-config-api")
        try:
            _validate_calendar_request(body)
        except LaunchplaneSafetyError as exc:
            raise ValueError("invalid_calendar_request") from exc
        if mode == "apply":
            digest = _reviewed_apply_digest(args)
            evidence, result = _load_reviewed_evidence(
                args,
                operation="product-config-dry-run",
                expected_digest=digest,
                digest_field="plan_digest",
                digest_result_key="odoo_business_hours",
            )
            request = _calendar_request(body)
            if evidence["request"].get("payload_digest") != request["payload_digest"]:
                raise ValueError("reviewed_dry_run_not_apply_eligible")
            calendar = _project_business_hours(result["odoo_business_hours"])
            _calendar_result_matches_request({**result, "odoo_business_hours": calendar}, {**request, "mode": "dry-run"})
            if not calendar["apply_eligible"] or calendar["operation"] == "inspect":
                raise ValueError("reviewed_dry_run_not_apply_eligible")
            body["reviewed_plan_sha256"] = digest
    elif getattr(args, "expected_plan_digest", "") or getattr(args, "dry_run_evidence_file", ""):
        raise ValueError("calendar_review_flags_require_calendar_request")
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


def _runtime_sync_result(source: object, *, mode: str, selected_keys: list[str] | None = None) -> dict[str, object]:
    result = _require_dict(source)
    if result.get("status") != "ok" or result.get("mode") != mode:
        raise LaunchplaneSafetyError("invalid_response")
    delta = _require_dict(result.get("runtime_environment"))
    projected_delta: dict[str, object] = {}
    for name in ("desired_key_count", "live_key_count", "unchanged_key_count"):
        value = delta.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise LaunchplaneSafetyError("invalid_response")
        projected_delta[name] = value
    for name in ("missing_keys", "different_keys", "changed_keys", "retired_keys_present"):
        values = delta.get(name, [] if name == "retired_keys_present" else None)
        if not isinstance(values, list) or len(values) > 500:
            raise LaunchplaneSafetyError("invalid_response")
        projected_delta[name] = sorted({_public_env_key(value) for value in values})
    changed = set(cast(list[str], projected_delta["changed_keys"]))
    expected = set(cast(list[str], projected_delta["missing_keys"])) | set(
        cast(list[str], projected_delta["different_keys"])
    ) | set(cast(list[str], projected_delta["retired_keys_present"]))
    if changed != expected:
        raise LaunchplaneSafetyError("invalid_response")
    if selected_keys is not None and (
        result.get("selected_keys") != selected_keys
        or changed - set(selected_keys)
        or projected_delta["retired_keys_present"]
        or projected_delta["desired_key_count"] != len(selected_keys)
    ):
        raise LaunchplaneSafetyError("invalid_response")
    target = _require_dict(result.get("tracked_target"))
    if not isinstance(target.get("target_id"), str) or not target["target_id"]:
        raise LaunchplaneSafetyError("invalid_response")
    target_type = public_code(target.get("target_type"))
    if target_type not in {"application", "compose"}:
        raise LaunchplaneSafetyError("invalid_response")
    target_digest = hashlib.sha256(json.dumps(
        [target_type, target["target_id"]], separators=(",", ":")
    ).encode()).hexdigest()
    apply = _require_dict(result.get("apply"))
    deploy = _require_dict(result.get("deploy"))
    if apply.get("applied") is not (mode == "apply") or not isinstance(apply.get("env_updated"), bool):
        raise LaunchplaneSafetyError("invalid_response")
    if deploy.get("requested") is not False or deploy.get("triggered") is not False:
        raise LaunchplaneSafetyError("invalid_response")
    verification = _require_dict(apply.get("verification"))
    verification_status = public_code(verification.get("status"))
    if verification_status not in {"pass", "skipped", "fail"}:
        raise LaunchplaneSafetyError("invalid_response")
    projected = {
        "status": "ok", "mode": mode,
        "context": public_identifier(result.get("context")),
        "instance": public_identifier(result.get("instance")),
        "target_sha256": target_digest,
        "runtime_environment": projected_delta,
        "env_updated": apply["env_updated"],
        "persistence_status": verification_status,
        "deploy_requested": False,
    }
    if selected_keys is not None:
        projected["selected_keys"] = selected_keys
    assert_public_safe_shape(projected)
    return projected


def _runtime_sync_plan_digest(request: dict[str, object], result: dict[str, object]) -> str:
    if not isinstance(result.get("target_sha256"), str) or not isinstance(result.get("runtime_environment"), dict):
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    return hashlib.sha256(json.dumps({
        "request": request, "target_sha256": result["target_sha256"],
        "runtime_environment": result["runtime_environment"],
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def execute_runtime_sync(args: argparse.Namespace) -> int:
    operation = args.command
    applying = operation.endswith("-apply")
    request: dict[str, object] = {
        "product": _request_identifier(args.product),
        "context": _request_identifier(args.context),
        "instance": _request_identifier(args.instance),
        "payload_source": "operator_argument",
    }
    try:
        selected_keys = sorted(_public_env_key(key) for key in args.key) if args.key else None
    except LaunchplaneSafetyError as error:
        raise ValueError("invalid_runtime_key_selection") from error
    if selected_keys is not None:
        if len(set(selected_keys)) != len(selected_keys):
            raise ValueError("invalid_runtime_key_selection")
        request["selected_keys"] = selected_keys
    reviewed_digest = ""
    if applying:
        _require_idempotency(args)
        if not args.reviewed_dry_run:
            raise ValueError("reviewed_dry_run_required")
        reviewed_digest = _required_lower_sha256(args.expected_plan_digest, code="invalid_expected_plan_digest")
        evidence, reviewed = _load_reviewed_evidence(
            args, operation="live-target-runtime-sync-dry-run", expected_digest=reviewed_digest
        )
        if evidence["request"] != request or _runtime_sync_plan_digest(request, reviewed) != reviewed_digest:
            raise ValueError("reviewed_dry_run_not_apply_eligible")
    settings = prepare_operator_settings(args=args, operation=operation, request=request)
    if settings is None:
        return 2
    apply_attempted = False
    apply_received = False
    known_trace = ""
    current_phase = "preflight"
    current_diagnostics: dict[str, object] = {}
    body = {"schema_version": 1, **{key: request[key] for key in ("product", "context", "instance")}, "deploy": False}
    if selected_keys is not None:
        body["selected_keys"] = selected_keys
    path = helper_command_path(operation)

    def call(mode: str, *, phase: str) -> tuple[dict[str, object], dict[str, Any]]:
        nonlocal known_trace, current_phase, current_diagnostics
        current_phase = phase
        current_diagnostics = {}
        try:
            response = request_launchplane(
                service_url=settings["service_url"], path=path, settings=settings,
                body={**body, "mode": mode}, timeout=args.timeout,
                idempotency_key=args.idempotency_key if mode == "apply" else "",
            )
        except (LaunchplaneSafetyError, ServiceResponseError):
            raise
        except ValueError:
            raise LaunchplaneSafetyError("invalid_response") from None
        current_diagnostics = service_response_diagnostics(response)
        if mode == "apply":
            known_trace = public_trace_id(response.get("trace_id"))
        if response.get("status") != "accepted":
            raise LaunchplaneSafetyError("invalid_response")
        projected = _runtime_sync_result(response.get("result"), mode=mode, selected_keys=selected_keys)
        if projected["context"] != request["context"] or projected["instance"] != request["instance"]:
            raise LaunchplaneSafetyError("invalid_response")
        return projected, response

    try:
        plan, raw = call("dry-run", phase="preflight")
        digest = _runtime_sync_plan_digest(request, plan)
        if applying and digest != reviewed_digest:
            raise ValueError("runtime_sync_plan_changed")
        result = plan
        if applying:
            apply_attempted = True
            result, raw = call("apply", phase="apply")
            apply_received = True
            known_trace = public_trace_id(raw.get("trace_id"))
            if _runtime_sync_plan_digest(request, result) != reviewed_digest:
                raise LaunchplaneSafetyError("invalid_response")
            observed, observed_raw = call("dry-run", phase="readback")
            delta = cast(dict[str, object], observed["runtime_environment"])
            matches = observed["target_sha256"] == result["target_sha256"] and delta["changed_keys"] == []
            if not matches or result["persistence_status"] == "fail":
                raise LaunchplaneSafetyError("invalid_response")
            result["read_back_matches"] = True
            result["read_back_trace_id"] = public_trace_id(observed_raw.get("trace_id"))
        result["plan_sha256"] = digest
        payload = base_payload(status="accepted", operation=operation, request=request)
        payload["result"] = result
        payload["summary"] = {
            "trace_id": public_trace_id(raw.get("trace_id")),
            "recommendation": "Review the key plan before apply; sync persists provider env and does not restart containers.",
        }
        emit(payload)
        return 0
    except (urllib.error.HTTPError, http.client.HTTPException, OSError, TimeoutError, urllib.error.URLError, LaunchplaneSafetyError, ValueError) as exc:
        if apply_attempted and not apply_received and isinstance(exc, urllib.error.HTTPError) and exc.code in {401, 403}:
            emit_http_error_payload(operation=operation, request=request, exc=exc)
        elif apply_attempted:
            payload = unavailable_payload(
                operation=operation, request=request, status="accepted_unverified",
                code="runtime_sync_apply_unverified",
                message="Runtime sync was attempted; inspect persistence before any retry.",
            )
            summary: dict[str, object] = {
                "trace_id": known_trace,
                "recommendation": "Run live-target-runtime-sync-dry-run for the same lane and compare target_sha256; zero changed_keys proves current provider persistence. Inspect the trace before retrying under any key; sync does not restart containers.",
            }
            if isinstance(exc, urllib.error.HTTPError):
                diagnostics = service_response_diagnostics(_error_payload_from_http(exc))
            elif isinstance(exc, LaunchplaneSafetyError):
                diagnostics = current_diagnostics
            else:
                diagnostics = service_response_diagnostics(exc)
            summary.update({("read_back_" if current_phase == "readback" else "") + key: value
                            for key, value in diagnostics.items()})
            payload["summary"] = summary
            emit(payload)
        elif isinstance(exc, urllib.error.HTTPError):
            emit_http_error_payload(operation=operation, request=request, exc=exc)
        elif isinstance(exc, LaunchplaneSafetyError):
            emit_safety_error_payload(operation=operation, request=request, exc=exc,
                                      provider_payload=ServiceResponseError(current_diagnostics))
        elif isinstance(exc, ServiceResponseError):
            emit_invalid_response(operation=operation, request=request, diagnostics=service_response_diagnostics(exc))
        elif isinstance(exc, ValueError):
            raise
        else:
            emit_read_transport_error(operation=operation, request=request, timeout=args.timeout, exc=exc)
        return 1


POLICY_PROPOSAL_DESCRIPTORS = (
    "production-backup-policy",
    "managed-authz-policy-set",
    "managed-merge-train-policy-import",
)


def privileged_policy_proposal_body(args: argparse.Namespace) -> dict[str, object]:
    body = read_payload_file(args.payload_file)
    if set(body) - {
        "schema_version",
        "descriptor_id",
        "source_event_id",
        "expires_in_seconds",
        "request",
    }:
        raise ValueError("invalid_proposal_envelope")
    if (
        body.get("schema_version", 1) != 1
        or body.get("descriptor_id") not in POLICY_PROPOSAL_DESCRIPTORS
    ):
        raise ValueError("unsupported_proposal_descriptor")
    source = body.get("source_event_id")
    if (
        not isinstance(source, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", source) is None
    ):
        raise ValueError("invalid_proposal_source_event")
    request_body = body.get("request")
    if not isinstance(request_body, dict):
        raise ValueError("invalid_proposal_request")
    reason = request_body.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        raise ValueError("invalid_proposal_reason")
    if body["descriptor_id"] == "managed-authz-policy-set":
        allowed = {
            "managed_set_id",
            "desired_policy",
            "schema_migration",
            "administrator_quorum_change",
            "workflow_rule_retirement",
            "reason",
            "related_issue",
        }
        required = {"managed_set_id", "desired_policy", "reason"}
    elif body["descriptor_id"] == "production-backup-policy":
        allowed = {"authority", "reason", "related_issue"}
        required = {"authority", "reason"}
        authority = request_body.get("authority")
        if not isinstance(authority, dict) or authority.get("mode") != "dry_run" or authority.get("reviewed_authority_digest"):
            raise ValueError("invalid_backup_proposal_authority")
        policy = authority.get("policy")
        product = policy.get("product") if isinstance(policy, dict) else None
        if not isinstance(product, str) or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", product) is None:
            raise ValueError("invalid_backup_proposal_product")
    else:
        allowed = {"record", "reason", "related_issue"}
        required = {"record", "reason"}
    if set(request_body) - allowed or not required.issubset(request_body):
        raise ValueError("invalid_proposal_request")
    return body


def _proposal_repository(value: str) -> str:
    repository = value.removesuffix(".git")
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise ValueError("repository_unresolved")
    return public_identifier(repository)


def _merge_train_proposal_request(args: argparse.Namespace) -> dict[str, object]:
    updated_at = args.updated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", updated_at) is None:
        raise ValueError("invalid_updated_at")
    return {
        "mode": "propose",
        "descriptor_id": "managed-merge-train-policy-import",
        "updated_at": updated_at,
        "candidate_record_id": f"merge-train-policy-{updated_at.replace('-', '').replace(':', '')}-proposal",
    }


def trusted_automation_proposal_request(args: argparse.Namespace) -> dict[str, object]:
    if args.github_user_id <= 0:
        raise ValueError("invalid_github_user_id")
    return {
        **_merge_train_proposal_request(args),
        "change": args.change,
        "github_user_id": args.github_user_id,
        "repositories": sorted({_proposal_repository(repository) for repository in args.repo}, key=str.casefold),
    }


def enrollment_proposal_request(args: argparse.Namespace) -> dict[str, object]:
    base_branches = args.base_branch or ["main"] * len(args.repo)
    if len(args.repository_id) != len(args.repo) or len(base_branches) != len(args.repo):
        raise ValueError("unpaired_enrollment_arguments")
    if any(repository_id <= 0 for repository_id in args.repository_id):
        raise ValueError("invalid_repository_id")
    enrollments = [
        {"repository": _proposal_repository(repository), "repository_id": repository_id, "base_branch": public_identifier(branch)}
        for repository, repository_id, branch in zip(args.repo, args.repository_id, base_branches, strict=True)
    ]
    if len({(entry["repository"].casefold(), entry["base_branch"]) for entry in enrollments}) != len(enrollments):
        raise ValueError("unpaired_enrollment_arguments")
    return {
        **_merge_train_proposal_request(args),
        "enrollments": enrollments,
        "like_repository": _proposal_repository(args.like),
        "like_base_branch": _request_identifier(args.like_base_branch),
    }


def _active_merge_train_policy(
    args: argparse.Namespace, provider_payload: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], list[Any]]:
    record = _require_dict(provider_payload.get("record"))
    policy = copy.deepcopy(_require_dict(record.get("policy")))
    targets = policy.get("policies")
    if provider_payload.get("status") != "ok" or record.get("status") != "active" or not isinstance(targets, list):
        raise LaunchplaneSafetyError("invalid_response")
    if args.expected_active_record_id and record.get("record_id") != args.expected_active_record_id:
        raise ValueError("active_policy_changed")
    return record, policy, targets


def _merge_train_proposal_envelope(
    *, args: argparse.Namespace, request: dict[str, object], active_record: dict[str, Any], policy: dict[str, Any]
) -> dict[str, object]:
    request["active_record_id"] = public_identifier(active_record.get("record_id"))
    request["active_policy_sha256"] = _project_sha256(active_record.get("policy_sha256"))
    return {
        "schema_version": 1,
        "descriptor_id": "managed-merge-train-policy-import",
        "source_event_id": args.source_event_id,
        "request": {
            "record": {
                "schema_version": 1,
                "record_id": request["candidate_record_id"],
                "status": "active",
                "source": "privileged-policy-proposal",
                "updated_at": request["updated_at"],
                "policy": policy,
            },
            "reason": args.reason,
            "related_issue": args.related_issue,
        },
    }


def _is_train_target(value: object, repository: object, base_branch: object) -> bool:
    target = _require_dict(value)
    name = target.get("repository")
    return (
        isinstance(name, str)
        and isinstance(repository, str)
        and name.casefold() == repository.casefold()
        and target.get("base_branch") == base_branch
    )


TEMPLATE_REPOSITORY_SETTINGS = ("provider_delivery_protection_expectation", "config_authority_events_enabled")


def _uses_only_github_app(github_token: dict[str, Any]) -> bool:
    other_sources = {name: value for name, value in github_token.items() if name != "github_app"}
    return isinstance(github_token.get("github_app"), dict) and not any(other_sources.values())


def enrollment_proposal_body(
    *, args: argparse.Namespace, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    record, policy, targets = _active_merge_train_policy(args, provider_payload)
    enrollments = cast(list[dict[str, Any]], request["enrollments"])
    templates = [
        target for target in targets if _is_train_target(target, request["like_repository"], request["like_base_branch"])
    ]
    if len(templates) != 1:
        raise ValueError("template_not_enrolled")
    if not _uses_only_github_app(_require_dict(templates[0].get("github_token"))):
        raise ValueError("template_not_github_app")
    for entry in enrollments:
        if any(_is_train_target(target, entry["repository"], entry["base_branch"]) for target in targets):
            raise ValueError("repository_already_enrolled")
    targets.extend(_enrollment_target(templates[0], entry) for entry in enrollments)
    request["changed_target_count"] = len(enrollments)
    return _merge_train_proposal_envelope(args=args, request=request, active_record=record, policy=policy)


def _enrollment_target(template: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    candidate = copy.deepcopy(template)
    for template_repository_setting in TEMPLATE_REPOSITORY_SETTINGS:
        candidate.pop(template_repository_setting, None)
    candidate["repository"] = entry["repository"]
    candidate["base_branch"] = entry["base_branch"]
    candidate["github_token"]["github_app"]["repository_id"] = entry["repository_id"]
    return candidate


def trusted_automation_proposal_body(
    *, args: argparse.Namespace, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    record, policy, targets = _active_merge_train_policy(args, provider_payload)
    requested = {repository.casefold() for repository in cast(list[str], request["repositories"])}
    matched: set[str] = set()
    changed_target_count = 0
    for value in targets:
        target = _require_dict(value)
        repository = target.get("repository")
        if not isinstance(repository, str) or repository.casefold() not in requested:
            continue
        matched.add(repository.casefold())
        enqueue = _require_dict(target.get("enqueue"))
        current = enqueue.get("trusted_automation_github_user_ids") or []
        if not isinstance(current, list) or not all(isinstance(user_id, int) for user_id in current):
            raise LaunchplaneSafetyError("invalid_response")
        user_id = int(str(request["github_user_id"]))
        candidate = sorted({*current, user_id}) if request["change"] == "add" else [entry for entry in current if entry != user_id]
        if candidate != current:
            enqueue["trusted_automation_github_user_ids"] = candidate
            changed_target_count += 1
    if requested - matched:
        raise ValueError("repository_not_enrolled")
    if not changed_target_count:
        raise ValueError("policy_unchanged")
    request["changed_target_count"] = changed_target_count
    return _merge_train_proposal_envelope(args=args, request=request, active_record=record, policy=policy)


MERGE_TRAIN_PROPOSALS = {
    "merge-train-trusted-automation-propose": (trusted_automation_proposal_request, trusted_automation_proposal_body),
    "merge-train-enrollment-propose": (enrollment_proposal_request, enrollment_proposal_body),
}


def execute_merge_train_proposal(args: argparse.Namespace) -> int:
    build_request, build_body = MERGE_TRAIN_PROPOSALS[args.command]
    request = build_request(args)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", args.source_event_id) is None:
        raise ValueError("invalid_proposal_source_event")
    if not args.reason.strip() or len(args.reason) > 240:
        raise ValueError("invalid_proposal_reason")
    settings = prepare_operator_settings(args=args, operation=args.command, request=request)
    if settings is None:
        return 2
    provider_payload = None
    try:
        provider_payload = request_launchplane_read(
            service_url=settings["service_url"],
            path=helper_command_path("merge-train-policy-read"),
            settings=settings,
            query={},
            timeout=args.timeout,
        )
        body = build_body(args=args, request=request, provider_payload=provider_payload)
    except urllib.error.HTTPError as exc:
        emit_http_error_payload(operation=args.command, request=request, exc=exc)
        return 1
    except LaunchplaneSafetyError as exc:
        emit_safety_error_payload(operation=args.command, request=request, exc=exc, provider_payload=provider_payload)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as exc:
        emit_read_transport_error(operation=args.command, request=request, timeout=args.timeout, exc=exc)
        return 1
    return execute_post(
        args=args,
        operation="privileged-policy-propose",
        path=helper_command_path("privileged-policy-propose"),
        request=request,
        body=body,
    )


def summarize_privileged_policy_proposal(
    *, request: dict[str, object], provider_payload: dict[str, object], service_url: str
) -> dict[str, object]:
    if (
        set(provider_payload) - {"status", "trace_id", "write_status", "summary"}
        or provider_payload.get("status") != "ok"
    ):
        raise LaunchplaneSafetyError("invalid_response")
    summary = _require_dict(provider_payload.get("summary"))
    descriptor = summary.get("descriptor_id")
    operation_id = summary.get("operation_id")
    if (
        descriptor != request["descriptor_id"]
        or not isinstance(operation_id, str)
        or re.fullmatch(r"privileged-operation-[0-9a-f]{32}", operation_id) is None
    ):
        raise LaunchplaneSafetyError("invalid_response")
    state = summary.get("status")
    if state not in {
        "planned",
        "approved",
        "revoked",
        "cancelled",
        "expired",
        "executing",
        "executed",
        "execution_failed",
    } or provider_payload.get("write_status") not in {"written", "replayed"}:
        raise LaunchplaneSafetyError("invalid_response")
    counts = () if descriptor == "production-backup-policy" else (
        (
            "added_rule_count",
            "adopted_rule_count",
            "updated_rule_count",
            "removed_rule_count",
            "unchanged_rule_count",
            "policy_safety_blocker_count",
            "operational_readiness_blocked_rule_count",
        )
        if descriptor == "managed-authz-policy-set"
        else (
            "active_target_count",
            "candidate_target_count",
            "unchanged_policy_key_count",
        )
    )
    normalized_expiry = _utc_timestamp(summary.get("expires_at"))
    result_status = summary.get("result_status")
    if result_status not in {"ok", "blocked"}:
        raise LaunchplaneSafetyError("invalid_response")
    if state == "planned":
        recommendation = (
            "The pending plan has blockers; review them at review_url before approval."
            if result_status == "blocked"
            else "Open review_url for the signed-in Director. The proposer cannot approve or apply."
        )
    elif state in {"approved", "executing"}:
        recommendation = "This plan is already approved or executing. Observe its existing outcome; no new approval is needed."
    else:
        recommendation = "This plan is terminal. Inspect its outcome instead of requesting approval. An intentional replacement needs a fresh envelope and source event."
    review_path = f"/ui/engineering/privileged-operations?operation_id={operation_id}"
    if descriptor == "production-backup-policy":
        product = request.get("product")
        if not isinstance(product, str) or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", product) is None:
            raise LaunchplaneSafetyError("invalid_response")
        review_path = f"/ui/products/{urllib.parse.quote(product, safe='')}/environments/prod/actions?backup_proposal={operation_id}#production-backup-authority"
        if summary.get("review_path") != review_path:
            raise LaunchplaneSafetyError("invalid_response")
    result = {
        "operation_id": operation_id,
        "descriptor_id": descriptor,
        "state": state,
        "result_status": result_status,
        "review_path": review_path,
        "review_url": build_launchplane_url(service_url, review_path),
        "expires_at": public_timestamp(normalized_expiry) if normalized_expiry else "",
        "counts": {name: _nonnegative_int(summary.get(name)) for name in counts},
        "authorizes_approval": False,
        "authorizes_execution": False,
    }
    if descriptor == "managed-authz-policy-set":
        if not isinstance(summary.get("changed"), bool):
            raise LaunchplaneSafetyError("invalid_response")
        result["changed"] = summary["changed"]
    payload = base_payload(
        status="accepted", operation="privileged-policy-propose", request=request
    )
    payload["result"] = result
    payload["summary"] = {
        "trace_id": public_trace_id(provider_payload.get("trace_id")),
        "write_status": provider_payload["write_status"],
        "recommendation": recommendation,
    }
    return payload


def _preview_feedback_response_unverified(
    *, request: dict[str, object], source: object, code: str,
) -> dict[str, object]:
    payload = unavailable_payload(
        operation="preview-feedback-remediation", request=request,
        status="accepted_unverified", code="apply_response_unverified",
        message="The apply response could not be verified; the comment may have changed.",
    )
    summary = cast(dict[str, object], payload["summary"])
    summary.update(service_response_diagnostics(source))
    summary["projection_error_code"] = code
    summary["recommendation"] = (
        "Run preview-feedback-remediation --mode dry-run for the same target to read back "
        "the comment state. A present comment does not prove whether a replacement completed. "
        "Inspect available trace or service records and resolve the response verification failure before any "
        "apply retry; retain the original request and idempotency key."
    )
    return payload


SIMPLE_APPLY_OPERATIONS = frozenset({
    "odoo-addon-settings-apply", "odoo-website-settings-apply",
    "integration-allowances-apply", "testing-hold-apply",
    "product-repository-identity-apply", "generic-web-deploy-recovery-apply",
    "repository-inventory-apply", "product-expected-config-apply",
    "service-runtime-setting-apply",
})


def _recovery_apply_readback(request: dict[str, object]) -> str:
    if request.get("reviewed_recovery_action") == "abandon_testing_original":
        return (
            "Re-run the same reference dry-run before retrying. Require completed, "
            "replay_completed and the saved recovery digest, then verify current-main "
            "post-deploy qualification. Original fixture residue may remain."
        )
    return "Verify the original reservation and a subsequent normal deploy before retrying or closing recovery."


def _simple_apply_response_unverified(
    *, operation: str, request: dict[str, object], source: object,
) -> dict[str, object]:
    payload = base_payload(
        status="accepted_unverified", operation=operation, request=request
    )
    recovery_apply = operation == "generic-web-deploy-recovery-apply"
    inventory_apply = operation == "repository-inventory-apply"
    payload["summary"] = {
        **service_response_diagnostics(source),
        "recommendation": (
            "Launchplane accepted the recovery apply, but its reply could not be verified. "
            + _recovery_apply_readback(request)
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
    return payload


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
    mutating = operation.endswith("-apply") or request.get("mode") == "apply" or request.get("mutate") is True
    provider_payload: dict[str, Any] | None = None
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
            if operation == "privileged-policy-propose":
                try:
                    payload = summarize_privileged_policy_proposal(
                        request=request, provider_payload=provider_payload,
                        service_url=settings["service_url"],
                    )
                except TypeError as exc:
                    raise LaunchplaneSafetyError("invalid_response") from exc
            else:
                payload = summarize_success(
                    operation=operation,
                    request=request,
                    provider_payload=provider_payload,
                )
            emit(payload)
        except LaunchplaneSafetyError as exc:
            if operation == "preview-feedback-remediation" and mutating:
                emit(_preview_feedback_response_unverified(
                    request=request, source=provider_payload, code=exc.code,
                ))
                return 1
            if operation == "privileged-policy-propose":
                payload = unavailable_payload(operation=operation, request=request, status="accepted_unverified", code="proposal_response_unverified", message="The proposal response could not be verified; the plan may have been saved.")
                payload["summary"] = {**service_response_diagnostics(provider_payload), "recommendation": "Retain and re-run the identical private envelope and source event. Replay returns the same plan; do not force a duplicate."}
                emit(payload)
                return 1
            if operation == "merge-train-controller-run-once":
                payload = unavailable_payload(
                    operation=operation, request=request, status="invalid",
                    code=exc.code, message="Launchplane returned an invalid response.",
                )
                summary = cast(dict[str, object], payload["summary"])
                summary.update(service_response_diagnostics(provider_payload))
                summary.update(_controller_error_diagnostics(provider_payload))
                summary["projection_error_code"] = exc.code
                http_status = getattr(provider_payload, "http_status", None)
                if (
                    provider_payload.get("status") == "accepted"
                    and type(http_status) is int and 200 <= http_status < 300
                ):
                    # Launchplane completed this request; only its reply is unreadable here.
                    summary["launchplane_status"] = "accepted"
                summary["recommendation"] = (
                    "The controller response could not be verified. Read the PR and controller "
                    "state before any retry; a mutating pass may have completed."
                    if request.get("mutate")
                    else "The controller dry-run response could not be verified; inspect the trace before retrying."
                )
                emit(payload)
                return 1
            if operation not in SIMPLE_APPLY_OPERATIONS:
                raise
            payload = _simple_apply_response_unverified(operation=operation, request=request, source=provider_payload)
            emit(payload)
        return 0
    except urllib.error.HTTPError as exc:
        if mutating and (exc.code >= 500 or exc.code == 408):
            payload = _apply_outcome_unknown(operation=operation, request=request, label="affected record and operation state")
            error_payload = _error_payload_from_http(exc)
            summary = _require_dict(payload["summary"])
            summary.update(service_response_diagnostics(error_payload))
            if operation == "merge-train-controller-run-once":
                summary.update(_controller_error_diagnostics(error_payload))
                summary["recommendation"] = (
                    "Read the PR and controller state before any retry; the mutating pass may have completed."
                )
            emit(payload)
            return 1
        emit_http_error_payload(operation=operation, request=request, exc=exc)
        return 1
    except LaunchplaneSafetyError as exc:
        emit_safety_error_payload(operation=operation, request=request, exc=exc,
                                  provider_payload=provider_payload)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as exc:
        if operation == "privileged-policy-propose":
            payload = unavailable_payload(operation=operation, request=request, status="outcome_unknown", code="proposal_transport_uncertain", message="The proposal response was interrupted; this does not prove a service outage or an unsaved plan.")
            payload["summary"] = {**service_response_diagnostics(exc), "recommendation": "Retain and re-run the identical private envelope and source event. Replay returns the same plan; do not force a duplicate."}
            emit(payload)
            return 1
        if operation == "merge-train-controller-run-once" and (
            isinstance(exc, TimeoutError)
            or (isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError))
        ):
            payload = unavailable_payload(
                operation=operation, request=request, status="unavailable",
                code="client_timeout", message=f"Controller request timed out after {args.timeout:g} s.",
            )
            payload["summary"] = {
                **service_response_diagnostics(exc),
                "error_code": "client_timeout", "timeout_seconds": args.timeout,
                "recommendation": (
                    "Read the PR and controller state before any retry; the mutating pass may have completed."
                    if request.get("mutate")
                    else "The client timed out; this does not establish a service outage."
                ),
            }
            emit(payload)
            return 1
        if mutating:
            payload = _apply_outcome_unknown(operation=operation, request=request, label="affected record and operation state")
            _require_dict(payload["summary"]).update(service_response_diagnostics(exc))
            emit(payload)
            return 1
        payload = unavailable_payload(operation=operation, request=request, status="unavailable",
                                      code="provider_unavailable", message="Launchplane service is unavailable.")
        _require_dict(payload["summary"]).update(service_response_diagnostics(exc))
        emit(payload)
        return 1
    except (ValueError, json.JSONDecodeError) as exc:
        if operation == "preview-feedback-remediation" and mutating and (
            isinstance(exc, ServiceResponseError) or provider_payload is not None
        ):
            emit(_preview_feedback_response_unverified(
                request=request,
                source=exc if isinstance(exc, ServiceResponseError) else provider_payload,
                code="invalid_response",
            ))
            return 1
        if operation == "privileged-policy-propose":
            payload = unavailable_payload(operation=operation, request=request, status="accepted_unverified", code="proposal_response_unverified", message="The proposal response could not be read; the plan may have been saved.")
            payload["summary"] = {**service_response_diagnostics(exc if isinstance(exc, ServiceResponseError) else provider_payload), "recommendation": "Retain and re-run the identical private envelope and source event. Replay returns the same plan; do not force a duplicate."}
            emit(payload)
            return 1
        if operation in SIMPLE_APPLY_OPERATIONS and isinstance(exc, ServiceResponseError):
            payload = _simple_apply_response_unverified(operation=operation, request=request, source=exc)
            emit(payload)
            return 1
        emit_safety_error_payload(operation=operation, request=request,
                                  exc=LaunchplaneSafetyError("invalid_response"),
                                  provider_payload=(exc if isinstance(exc, ServiceResponseError)
                                                    else provider_payload))
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
    provider_payload = None
    try:
        provider_payload = request_launchplane_read(
            service_url=settings["service_url"],
            path=internal_helper_path("merge-train-policy-targets-read"),
            settings=settings,
            query={},
            timeout=args.timeout,
        )
        current_policy = _project_merge_train_policy_targets(provider_payload)
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
        provider_payload = None
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
            payload = base_payload(
                status="accepted_unverified", operation=operation, request=request
            )
            payload["result"] = {"current_policy": current_policy}
            payload["summary"] = {
                **service_response_diagnostics(provider_payload),
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
        if operation == "merge-train-policy-import-apply" and post_attempted and (exc.code >= 500 or exc.code == 408):
            payload = _apply_outcome_unknown(operation=operation, request=request, label="active merge-train policy")
            _require_dict(payload["summary"]).update(service_response_diagnostics(_error_payload_from_http(exc)))
            emit(payload)
            return 1
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
        emit_safety_error_payload(operation=operation, request=request, exc=exc, provider_payload=provider_payload)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as exc:
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
            cast(dict[str, object], payload["summary"]).update(service_response_diagnostics(exc))
            emit(payload)
            return 1
        emit_read_transport_error(operation=operation, request=request, timeout=args.timeout, exc=exc)
        return 1
    except (ValueError, json.JSONDecodeError) as exc:
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
            cast(dict[str, object], payload["summary"]).update(service_response_diagnostics(
                exc if isinstance(exc, ServiceResponseError) else provider_payload))
            emit(payload)
            return 0
        emit_invalid_response(operation=operation, request=request, diagnostics=service_response_diagnostics(
            exc if isinstance(exc, ServiceResponseError) else provider_payload))
        return 1


def execute_dokploy_inventory_read(*, args: argparse.Namespace) -> int:
    operation = "dokploy-inventory-read"
    request: dict[str, object] = {"offset": args.offset, "limit": args.limit}
    settings = prepare_operator_settings(args=args, operation=operation, request=request)
    if settings is None:
        return 2
    query = {"offset": str(args.offset), "limit": str(args.limit)}
    if args.inventory_digest:
        query["inventory_digest"] = args.inventory_digest
    provider_payload = None
    try:
        provider_payload = request_launchplane_read(
            service_url=settings["service_url"], path=helper_command_path(operation),
            settings=settings, query=query, timeout=args.timeout,
        )
        result, trace_id = project_inventory(provider_payload)
        if result["offset"] != args.offset or result["limit"] != args.limit or (args.inventory_digest and result["inventory_digest"] != args.inventory_digest):
            raise LaunchplaneSafetyError("invalid_response")
        payload = base_payload(status="ok", operation=operation, request=request)
        payload["result"] = result
        payload["summary"] = {"trace_id": trace_id, "recommendation": "Follow next_offset with the same inventory_digest until null. Restart at zero on inventory change. Placement names are provider labels; dokploy_host has no verified guest hostname."}
        emit(payload)
        return 0
    except urllib.error.HTTPError as exc:
        emit_http_error_payload(operation=operation, request=request, exc=exc)
    except LaunchplaneSafetyError as exc:
        emit_safety_error_payload(operation=operation, request=request, exc=exc, provider_payload=provider_payload)
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as exc:
        emit_read_transport_error(operation=operation, request=request, timeout=args.timeout, exc=exc)
    except ServiceResponseError as exc:
        emit_safety_error_payload(operation=operation, request=request, exc=LaunchplaneSafetyError("invalid_response"), provider_payload=exc)
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
    provider_payload = None
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
        emit_safety_error_payload(operation=operation, request=request, exc=exc, provider_payload=provider_payload)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as exc:
        emit_read_transport_error(
            operation=operation, request=request, timeout=args.timeout, exc=exc,
        )
        return 1
    except (ValueError, json.JSONDecodeError) as exc:
        emit_invalid_response(operation=operation, request=request, diagnostics=service_response_diagnostics(
            exc if isinstance(exc, ServiceResponseError) else provider_payload))
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
    "client_before",
    "client_after",
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
DOKPLOY_COMPOSE_DOMAIN_COMMANDS = {
    f"dokploy-target-{operation}-{mode}"
    for operation in ("reconcile-compose-domain", "prune-compose-domain")
    for mode in ("dry-run", "apply")
}
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
DOKPLOY_APPLICATION_COMMANDS = {
    "dokploy-target-create-application-dry-run",
    "dokploy-target-create-application-apply",
}
# A testing application in new placement; ids, domains, git source and app names
# belong to the service or provider and are refused.
DOKPLOY_APPLICATION_PAYLOAD_FIELDS = {
    "schema_version",
    "context",
    "instance",
    "target_name",
    "server_id",
    "project_name",
    "project_description",
    "environment_name",
    "environment_description",
    "description",
    "source_git_ref",
    "healthcheck_path",
    "deploy_timeout_seconds",
    "state_volume_path",
    "expected_current_provider_target",
    "reason",
}
# With a state volume the route answers with its saved plan, the adopted target
# or a retained-custody receipt instead of the ordinary setup result.
DOKPLOY_APPLICATION_STATE_VOLUME_RESULT_FIELDS = {
    "operation",
    "context",
    "instance",
    "applied",
    "deployment_performed",
    "target",
    "allocation",
    "state_volume_plan_id",
    "plan_sha256",
    "state_volume",
    "configuration_matches",
    "configuration_evidence",
    "allocation_state",
    "setup",
}
DOKPLOY_APPLICATION_RETAINED_RESULT_FIELDS = {
    "applied",
    "allocation_state",
    "original_key",
    "plan_id",
    "deployment_performed",
    "resources_retained",
    "reviewed_custody_sha256",
    "retained_provider_effect_phase",
}
# Recovery of an uncertain storage allocation: readback reports custody without
# creating or adopting, and retain keeps that exact custody under the original key.
DOKPLOY_APPLICATION_READBACK_COMMAND = "dokploy-target-create-application-readback"
DOKPLOY_APPLICATION_RETAIN_COMMAND = "dokploy-target-create-application-retain"
DOKPLOY_APPLICATION_READBACK_RESULT_FIELDS = DOKPLOY_APPLICATION_STATE_VOLUME_RESULT_FIELDS - {
    "setup",
} | {"operation_state", "provider_effect_phase", "custody_sha256"}
DOKPLOY_APPLICATION_CONFIGURATION_EVIDENCE = {"matches", "differs", "unavailable"}
# The durable phase names generated provider ids; only its step names are shown.
STATE_VOLUME_CREATED_STEPS = ("project", "environment", "application")
STATE_VOLUME_PENDING_STEPS = {
    "/api/project.create": "project",
    "/api/environment.create": "environment",
    "/api/application.create": "application",
    "/api/mounts.create": "mount",
}
# Plain next steps for the service's stable storage refusal codes; never parse messages.
APPLICATION_STATE_VOLUME_NEXT_STEPS = {
    "application_state_volume_execution_active": (
        "The original execution lease is still active. Wait for it to expire, then retain "
        "again with the same readback evidence and original key."
    ),
    "application_state_volume_lane_busy": (
        "The testing lane executor is busy. Retry the original operation with the original "
        "key after it releases the lane."
    ),
    "application_state_volume_custody_changed": (
        "Custody changed since the reviewed readback. Read back again with the original key "
        "and plan, review the new custody_sha256, then retain with it."
    ),
    "application_state_volume_already_adopted": (
        "The allocation is already the tracked target. Do not retain; apply again with the "
        "original key and saved evidence to complete the adoption receipt."
    ),
    "application_state_volume_owner_changed": (
        "Context ownership changed. Resolve current ownership through the existing authority "
        "process before retrying."
    ),
    "application_state_volume_unqualified": (
        "The request does not match the original plan and key. Use the unchanged private "
        "payload, its saved dry-run evidence and the original key; new work needs a fresh dry run."
    ),
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
        "owner_before": _project_owner_identity(
            _client_or_legacy_owner(source, "client_before", "owner_before")
        ),
        "owner_after": _project_owner_identity(
            _client_or_legacy_owner(source, "client_after", "owner_after")
        ),
        "changed": bool(_optional_bool(source.get("changed"))),
        "applied": bool(_optional_bool(source.get("applied"))),
        "reason": public_operator_text(source.get("reason")),
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
    if value is None or value == "":
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
        "reason": public_operator_text(source.get("reason")),
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


def _project_dokploy_application_setup(
    result: object, *, request: dict[str, object] | None
) -> dict[str, object]:
    """Plan actions or the service's storage plan, the receipt state and provider kinds;
    provider ids, server ids, project, environment, application and volume names drop."""
    request = request or {}
    source = _require_dict(result)
    payload_digest = request.get("payload_digest")
    if not isinstance(payload_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", payload_digest):
        raise LaunchplaneSafetyError("invalid_response")
    mode = _reviewed_plan_mode(request.get("mode"))
    state_volume = request.get("state_volume") is True
    projected: dict[str, object]
    if state_volume and source.get("allocation_state") == "retained_uncertain":
        # A retained receipt is not a created target; finish reports it.
        if any(str(key) not in DOKPLOY_APPLICATION_RETAINED_RESULT_FIELDS for key in source):
            raise LaunchplaneSafetyError("unsafe_response_shape")
        projected = {
            "mode": mode,
            "operation": "create-application",
            "applied": False,
            "allocation_state": "retained_uncertain",
        }
        assert_public_safe_shape(projected)
        return projected
    allowed = (
        DOKPLOY_APPLICATION_STATE_VOLUME_RESULT_FIELDS
        if state_volume
        else DOKPLOY_COMPOSE_SETUP_RESULT_FIELDS
    )
    if any(str(key) not in allowed for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    if source.get("operation") != "create-application" or source.get("instance") != "testing":
        raise LaunchplaneSafetyError("invalid_response")
    if not state_volume and source.get("mode") != mode:
        raise LaunchplaneSafetyError("invalid_response")
    setup = source.get("setup")
    setup = {} if setup is None else _require_dict(setup)
    projected = {
        "mode": mode,
        "operation": "create-application",
        "context": public_identifier(source.get("context")),
        "instance": "testing",
        "applied": bool(_optional_bool(source.get("applied"))),
    }
    if state_volume:
        plan_id = source.get("state_volume_plan_id")
        service_plan_sha256 = source.get("plan_sha256")
        if (
            not isinstance(plan_id, str) or not re.fullmatch(r"[0-9a-f]{32}", plan_id)
            or not isinstance(service_plan_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", service_plan_sha256)
            or source.get("deployment_performed") is not False
        ):
            raise LaunchplaneSafetyError("invalid_response")
        # The service binds each apply request to this saved plan; the helper sends both back.
        projected["state_volume"] = {
            "plan_id": plan_id,
            "service_plan_sha256": service_plan_sha256,
        }
        if source.get("allocation_state") is not None:
            projected["allocation_state"] = public_code(source.get("allocation_state"))
            projected["configuration_evidence"] = public_code(
                source.get("configuration_evidence"), default="unavailable"
            )
        digest_inputs: dict[str, object] = {"state_volume": projected["state_volume"]}
    else:
        plan = _require_dict(setup.get("plan"))
        plan_actions = {
            part: public_code(_require_dict(plan.get(part)).get("action"))
            for part in ("project", "environment", "application")
        }
        projected["plan_actions"] = plan_actions
        digest_inputs = {"plan_actions": plan_actions}
    provider_target = setup.get("provider_target_record")
    if provider_target is not None:
        provider_target = _require_dict(provider_target)
        projected["provider_target"] = {
            field: public_code(provider_target.get(field))
            for field in ("provider_id", "target_category", "provider_target_type")
        }
    projected["plan_sha256"] = _application_plan_digest(
        payload_digest, cast(str, projected["context"]), digest_inputs
    )
    assert_public_safe_shape(projected)
    return projected


def _project_dokploy_application_readback(
    result: object, *, request: dict[str, object] | None
) -> dict[str, object]:
    """Original-key custody evidence for the reviewed storage plan; generated names,
    provider ids and the server id drop, and the durable phase keeps only step names."""
    request = request or {}
    source = _require_dict(result)
    payload_digest = request.get("payload_digest")
    if not isinstance(payload_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", payload_digest):
        raise LaunchplaneSafetyError("invalid_response")
    if any(str(key) not in DOKPLOY_APPLICATION_READBACK_RESULT_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    evidence = source.get("configuration_evidence")
    custody = source.get("custody_sha256")
    if (
        source.get("operation") != "create-application"
        or source.get("instance") != "testing"
        or source.get("deployment_performed") is not False
        # Readback answers for the plan it was sent, never another one.
        or source.get("state_volume_plan_id") != request.get("state_volume_plan_id")
        or source.get("plan_sha256") != request.get("service_plan_sha256")
        or evidence not in DOKPLOY_APPLICATION_CONFIGURATION_EVIDENCE
        or source.get("configuration_matches") is not (evidence == "matches")
        or not isinstance(custody, str)
        or not re.fullmatch(r"[0-9a-f]{64}", custody)
    ):
        raise LaunchplaneSafetyError("invalid_response")
    state_volume = {
        "plan_id": cast(str, request["state_volume_plan_id"]),
        "service_plan_sha256": cast(str, request["service_plan_sha256"]),
    }
    context = public_identifier(source.get("context"))
    if context != request.get("context"):
        raise LaunchplaneSafetyError("invalid_response")
    projected: dict[str, object] = {
        "mode": "readback",
        "operation": "create-application",
        "context": context,
        "instance": "testing",
        "applied": bool(_optional_bool(source.get("applied"))),
        "state_volume": state_volume,
        "operation_state": public_code(source.get("operation_state")),
        "provider_effect_phase": _state_volume_phase_steps(source.get("provider_effect_phase")),
        "configuration_matches": evidence == "matches",
        "configuration_evidence": evidence,
        "custody_sha256": custody,
        "allocation_state": public_code(source.get("allocation_state"), default="unknown"),
        # The same review digest as the dry run, so retention can bind this readback to it.
        "plan_sha256": _application_plan_digest(
            payload_digest, context, {"state_volume": state_volume}
        ),
    }
    assert_public_safe_shape(projected)
    return projected


def _state_volume_phase_steps(value: object) -> dict[str, object]:
    if value is None or value == "":
        return {"recorded": False}
    if not isinstance(value, str):
        raise LaunchplaneSafetyError("invalid_response")
    try:
        phase = json.loads(value)
    except json.JSONDecodeError:
        raise LaunchplaneSafetyError("invalid_response") from None
    if not isinstance(phase, dict):
        raise LaunchplaneSafetyError("invalid_response")
    pending = phase.get("pending")
    return {
        "recorded": True,
        "created": [step for step in STATE_VOLUME_CREATED_STEPS if phase.get(step)],
        "mount_verified": phase.get("mount_verified") == "true",
        "pending": (
            STATE_VOLUME_PENDING_STEPS.get(pending, "unknown")
            if isinstance(pending, str)
            else "none" if pending is None else "unknown"
        ),
    }


def _application_plan_digest(
    payload_digest: str, context: str, digest_inputs: dict[str, object]
) -> str:
    # Bind the private payload to the reviewed plan, as for compose creation.
    return _canonical_sha256(
        {
            "payload_digest": payload_digest,
            "operation": "create-application",
            "context": context,
            "instance": "testing",
            **digest_inputs,
        }
    )


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
        or public_operator_text(result.get("reason")) != public_operator_text(body["reason"])
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
        or public_operator_text(result.get("reason")) != public_operator_text(body["reason"])
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


def compose_domain_body(
    args: argparse.Namespace, *, mode: str
) -> tuple[dict[str, object], dict[str, object]]:
    payload = read_payload_file(args.payload_file)
    operation = args.command.removeprefix("dokploy-target-").removesuffix("-apply").removesuffix("-dry-run")
    allowed = {"schema_version", "context", "instance", "domains", "reason"}
    if operation == "reconcile-compose-domain":
        allowed.add("runtime_port")
    if payload.keys() - allowed:
        raise ValueError("unsupported_dokploy_target_field")
    for field in ("context", "instance", "reason"):
        if not isinstance(payload.get(field), str) or not str(payload[field]).strip():
            raise ValueError(f"{field}_required")
    domains = payload.get("domains")
    if (
        not isinstance(domains, list) or not 1 <= len(domains) <= 100
        or any(not isinstance(domain, str) or len(domain) > 253 or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*", domain
        ) for domain in domains)
        or len(set(domains)) != len(domains)
    ):
        raise ValueError("invalid_compose_domains")
    if operation == "reconcile-compose-domain":
        port = payload.get("runtime_port")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid_runtime_port")
    request: dict[str, object] = {
        "mode": mode, "payload_source": "private_file",
        "payload_digest": metadata_review_digest(payload),
        "context": public_identifier(payload["context"]),
        "instance": public_identifier(payload["instance"]),
        "setup_operation": operation,
        "domain_count": len(domains),
        "runtime_port": payload.get("runtime_port"),
    }
    if mode == "apply":
        evidence, reviewed = _load_reviewed_evidence(
            args, operation=args.command.removesuffix("-apply") + "-dry-run",
            expected_digest=_reviewed_apply_digest(args), result_status=None,
        )
        if (
            evidence["request"].get("payload_digest") != request["payload_digest"]
            or reviewed.get("operation") != operation
            or reviewed.get("applied") is not False
            or not isinstance(reviewed.get("binding_sha256"), str)
        ):
            raise ValueError("reviewed_dry_run_not_apply_eligible")
    body = {**payload, "operation": operation, "product": "launchplane", "mode": mode}
    if mode == "apply":
        body["confirmation"] = DOKPLOY_TARGET_SETUP_CONFIRMATION
    return body, request


def _project_compose_domain_setup(
    result: object, *, request: dict[str, object]
) -> dict[str, object]:
    source = _require_dict(result)
    if source.keys() - DOKPLOY_COMPOSE_SETUP_RESULT_FIELDS:
        raise LaunchplaneSafetyError("unsafe_response_shape")
    operation = source.get("operation")
    setup = _require_dict(source.get("setup"))
    domains = setup.get("domains")
    target_id = _dokploy_setup_target_id(source)
    target = _require_dict(setup.get("target_record"))
    if (
        operation != request.get("setup_operation") or not target_id
        or target.get("target_type") != "compose"
        or source.get("context") != request.get("context")
        or source.get("instance") != request.get("instance")
        or not isinstance(domains, list) or not all(isinstance(domain, str) for domain in domains)
        or len(domains) != request.get("domain_count")
        or source.get("mode") != request.get("mode")
        or source.get("applied") is not (request.get("mode") == "apply")
        or setup.get("applied") is not source.get("applied")
        or (operation == "reconcile-compose-domain" and setup.get("runtime_port") != request.get("runtime_port"))
    ):
        raise LaunchplaneSafetyError("invalid_response")
    projected = {
        "mode": source["mode"], "operation": operation,
        "context": request["context"], "instance": request["instance"],
        "applied": source["applied"], "domain_count": len(domains),
        "runtime_port": request.get("runtime_port"),
        "binding_sha256": _canonical_sha256(target_id),
    }
    for field in ("matched_domain_ids", "deleted_domain_ids", "missing_domains", "warnings"):
        values = setup.get(field, [])
        if not isinstance(values, list) or len(values) > 1000:
            raise LaunchplaneSafetyError("invalid_response")
        projected[field.removesuffix("s") + "_count"] = len(values)
    projected["plan_sha256"] = _canonical_sha256({
        "payload_digest": request["payload_digest"],
        "operation": operation, "binding_sha256": projected["binding_sha256"],
    })
    assert_public_safe_shape(projected)
    return projected


def execute_compose_domain_apply(
    *, args: argparse.Namespace, request: dict[str, object], body: dict[str, object]
) -> int:
    _, reviewed = _load_reviewed_evidence(
        args, operation=args.command.removesuffix("-apply") + "-dry-run",
        expected_digest=_reviewed_apply_digest(args), result_status=None,
    )
    before: dict[str, object] = {}
    requested_domains = _domain_set(body["domains"])
    reconcile = body["operation"] == "reconcile-compose-domain"

    def read(settings: dict[str, str]) -> tuple[dict[str, object], dict[str, object]]:
        return read_dokploy_target(
            settings=settings, context=cast(str, body["context"]),
            instance=cast(str, body["instance"]), timeout=args.timeout,
        )

    def binding_matches(public: dict[str, object], private: dict[str, object]) -> bool:
        ids = cast(set[str], private["target_ids"])
        return (
            public["status"] == "ok" and public["target_type"] == "compose"
            and public["provider_target_record"] == "present" and len(ids) == 1
            and private["live_target_type"] == "compose"
            and private["live_target_id"] in ids
            and all(target_id and _canonical_sha256(target_id) == reviewed["binding_sha256"] for target_id in ids)
        )

    def preflight(settings: dict[str, str]) -> dict[str, object] | None:
        public, private = read(settings)
        if not binding_matches(public, private):
            return {
                "error_code": "compose_binding_changed_since_review",
                "recommendation": "The tracked binding changed since review; run a new dry-run.",
            }
        before.update(private)
        return None

    def finish(settings: dict[str, str], _provider: dict[str, Any], payload: dict[str, Any]) -> bool:
        result = payload["result"]
        applied_as_reviewed = result["plan_sha256"] == args.expected_plan_digest.strip().lower()
        expected_domains = cast(frozenset[str], before["domains"])
        expected_domains = expected_domains | requested_domains if reconcile else expected_domains - requested_domains

        def read_back() -> dict[str, object]:
            public, private = read(settings)
            live = private["live_domains"]
            if not isinstance(live, list) or not all(isinstance(route, dict) and isinstance(route.get("host"), str) for route in live):
                raise ServiceResponseError(cast(dict[str, object], private["response_diagnostics"]))
            routes = live
            routes_by_host = {
                domain: [route for route in routes if str(route["host"]).strip().lower() == domain]
                for domain in requested_domains
            }
            routes_match = all(
                bool(matches) and all(
                    route.get("port") == body["runtime_port"] and route.get("https") is True
                    for route in matches
                )
                for matches in routes_by_host.values()
            ) if reconcile else not any(routes_by_host.values())
            return {
                **public, "binding_matches_review": binding_matches(public, private),
                "tracked_domains_match": private["domains"] == expected_domains,
                "provider_routes_match": routes_match,
            }

        read_back_ok = attach_read_back(
            payload, read=read_back,
            matches=lambda observed: all(observed[field] is True for field in (
                "binding_matches_review", "tracked_domains_match", "provider_routes_match",
            )), label="compose domain routes",
        )
        return applied_as_reviewed and read_back_ok

    return execute_verified_apply(
        args=args, operation=args.command, request=request,
        path=helper_command_path(args.command), body=body,
        preflight=preflight, finish=finish, label="compose domain routes",
    )


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
    # Private targets need no web route. Stable-lane repair validates the
    # product's public/private declaration against its tracked target.
    if (
        not isinstance(domains, list)
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


def dokploy_application_payload(args: argparse.Namespace) -> dict[str, object]:
    payload = read_payload_file(args.payload_file)
    if any(key not in DOKPLOY_APPLICATION_PAYLOAD_FIELDS for key in payload):
        raise ValueError("unsupported_dokploy_target_field")
    for field in ("context", "instance", "target_name", "server_id", "project_name", "environment_name", "reason"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field}_required")
    # The setup route creates application targets only for a lane nobody uses yet.
    if payload["instance"] != "testing":
        raise ValueError("application_target_requires_testing")
    healthcheck_path = payload.get("healthcheck_path", "")
    if not isinstance(healthcheck_path, str) or (healthcheck_path and not healthcheck_path.startswith("/")):
        raise ValueError("invalid_healthcheck_path")
    # Launchplane owns the rest of the state directory rules.
    state_volume_path = payload.get("state_volume_path")
    if state_volume_path is not None and (
        not isinstance(state_volume_path, str)
        or not state_volume_path.startswith("/")
        or ".." in state_volume_path.split("/")
    ):
        raise ValueError("invalid_state_volume_path")
    expected_target = payload.get("expected_current_provider_target")
    if expected_target is not None and not isinstance(expected_target, dict):
        raise ValueError("invalid_expected_current_provider_target")
    return payload


def dokploy_application_body(
    args: argparse.Namespace, *, mode: str
) -> tuple[dict[str, object], dict[str, object]]:
    payload = dokploy_application_payload(args)
    request: dict[str, object] = {
        "mode": mode,
        "payload_source": "private_file",
        "payload_digest": metadata_review_digest(payload),
        "context": public_identifier(payload["context"]),
        "instance": "testing",
        "setup_operation": "create-application",
        "state_volume": bool(payload.get("state_volume_path")),
    }
    body: dict[str, object] = {
        **payload,
        "operation": "create-application",
        # Target setup is authorized on Launchplane's own service product.
        "product": "launchplane",
        "mode": mode,
    }
    if mode in {"apply", "readback"}:
        state_volume = _reviewed_application_plan(args, request)
        if isinstance(state_volume, dict):
            # Apply and recovery reuse the service's saved storage plan; a new dry run
            # would allocate different names, so the reviewed one is the only one sent.
            body["state_volume_plan_id"] = state_volume["plan_id"]
            body["expected_plan_sha256"] = state_volume["service_plan_sha256"]
        if mode == "readback":
            # Readback evidence names the plan and original key it answers for, so
            # retention can require the same ones.
            request.update(
                state_volume_plan_id=body["state_volume_plan_id"],
                service_plan_sha256=body["expected_plan_sha256"],
                idempotency_key_fingerprint=repository_inventory_idempotency_key_fingerprint(
                    args.idempotency_key
                ),
            )
        if mode == "apply":
            body["confirmation"] = DOKPLOY_TARGET_SETUP_CONFIRMATION
    return body, request


def _reviewed_application_plan(
    args: argparse.Namespace, request: dict[str, object]
) -> dict[str, object] | None:
    """The saved storage plan, or None without storage, when the dry-run evidence
    matches this payload and its recomputed digest is the reviewed one."""
    if request["mode"] == "readback":
        # Readback only observes, so it needs no apply acknowledgement; recording the
        # original key's fingerprint in the request requires that key.
        expected_plan_digest = args.expected_plan_digest.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_plan_digest):
            raise ValueError("invalid_expected_plan_digest")
    else:
        expected_plan_digest = _reviewed_apply_digest(args)
    evidence, result = _load_reviewed_evidence(
        args,
        operation="dokploy-target-create-application-dry-run",
        expected_digest=expected_plan_digest,
        result_status=None,
    )
    if (
        evidence["request"].get("payload_digest") != request["payload_digest"]
        or evidence["request"].get("state_volume") is not request["state_volume"]
    ):
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    # Recompute the digest from this payload and the saved plan before sending, so
    # edited evidence cannot carry the reviewed digest onto a different plan.
    state_volume = result.get("state_volume")
    if request["state_volume"]:
        if not isinstance(state_volume, dict):
            raise ValueError("reviewed_dry_run_not_apply_eligible")
        digest_inputs: dict[str, object] = {"state_volume": {
            "plan_id": state_volume.get("plan_id"),
            "service_plan_sha256": state_volume.get("service_plan_sha256"),
        }}
    elif request["mode"] == "readback":
        # Only a storage allocation has original-key custody to read back.
        raise ValueError("state_volume_plan_required")
    else:
        digest_inputs = {"plan_actions": result.get("plan_actions")}
    if expected_plan_digest != _application_plan_digest(
        cast(str, request["payload_digest"]), cast(str, request["context"]), digest_inputs
    ):
        raise ValueError("reviewed_dry_run_not_apply_eligible")
    return state_volume if isinstance(state_volume, dict) else None


def dokploy_application_retain_body(
    args: argparse.Namespace,
) -> tuple[dict[str, object], dict[str, object]]:
    """Explicit custody retention: the reviewed apply, under the original key, with the
    custody digest from that key's saved readback evidence."""
    body, request = dokploy_application_body(args, mode="apply")
    if not request["state_volume"]:
        raise ValueError("state_volume_plan_required")
    expected_custody = str(args.expected_custody_digest or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_custody):
        raise ValueError("invalid_expected_custody_digest")
    key_fingerprint = repository_inventory_idempotency_key_fingerprint(args.idempotency_key)
    try:
        evidence = read_payload_file(str(args.readback_evidence_file or "").strip())
    except ValueError:
        raise ValueError("reviewed_readback_not_retain_eligible") from None
    readback = evidence.get("result")
    readback_request = evidence.get("request")
    if (
        not isinstance(readback, dict)
        or not isinstance(readback_request, dict)
        or evidence.get("operation") != DOKPLOY_APPLICATION_READBACK_COMMAND
        or evidence.get("status") != "accepted"
        or readback.get("mode") != "readback"
        # The same payload, review, plan and original key as this retention.
        or readback_request.get("payload_digest") != request["payload_digest"]
        or readback_request.get("idempotency_key_fingerprint") != key_fingerprint
        or readback.get("plan_sha256") != args.expected_plan_digest.strip().lower()
        or readback.get("state_volume") != {
            "plan_id": body["state_volume_plan_id"],
            "service_plan_sha256": body["expected_plan_sha256"],
        }
        or readback.get("custody_sha256") != expected_custody
    ):
        raise ValueError("reviewed_readback_not_retain_eligible")
    body["retain_state_volume_allocation"] = True
    body["expected_state_volume_custody_sha256"] = expected_custody
    request.update(
        retain_state_volume_allocation=True,
        expected_custody_sha256=expected_custody,
        state_volume_plan_id=body["state_volume_plan_id"],
        idempotency_key_fingerprint=key_fingerprint,
    )
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
    try:
        profile = _require_dict(provider_payload.get("profile"))
        return _project_owner_identity(_client_or_legacy_owner(profile, "client", "owner"))
    except (LaunchplaneSafetyError, ValueError, TypeError) as exc:
        raise ServiceResponseError(service_response_diagnostics(provider_payload)) from exc


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
    try:
        profile = _require_dict(provider_payload.get("profile"))
        image = _optional_dict(profile.get("image")) or {}
        repository = image.get("repository") or ""
        if not isinstance(repository, str):
            raise LaunchplaneSafetyError("invalid_response")
        return _project_image_repository(
            repository.strip().rstrip("/"), pattern=IMAGE_REPOSITORY_RE
        )
    except (LaunchplaneSafetyError, ValueError, TypeError) as exc:
        raise ServiceResponseError(service_response_diagnostics(provider_payload)) from exc


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
    try:
        _require_read_fields("production-backup-authority-read", provider_payload)
        return _project_production_backup_authority_read(provider_payload.get("authority"))
    except (LaunchplaneSafetyError, ValueError, TypeError) as exc:
        raise ServiceResponseError(service_response_diagnostics(provider_payload)) from exc


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
    try:
        inspect = _require_dict(provider_payload.get("inspect"))
        tracked = _require_dict(inspect.get("tracked_target") or {})
        provider_target = _require_dict(inspect.get("provider_target_record") or {})
        live_provider = _require_dict(inspect.get("provider") or {})
        live_target_id = live_provider.get("id")
        if live_target_id is not None and not isinstance(live_target_id, str):
            raise LaunchplaneSafetyError("invalid_response")
        private = {
            "target_ids": {
                str(source.get("target_id") or "").strip()
                for source in (inspect, tracked, provider_target)
            },
            "domains": _domain_set(tracked.get("domains")),
            "healthcheck_path": str(tracked.get("healthcheck_path") or ""),
            "live_domains": live_provider.get("domains", []),
            "response_diagnostics": service_response_diagnostics(provider_payload),
            "live_target_id": live_target_id,
            "live_target_type": live_provider.get("target_type"),
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
    except (LaunchplaneSafetyError, ValueError, TypeError) as exc:
        raise ServiceResponseError(service_response_diagnostics(provider_payload)) from exc


def attach_read_back(
    payload: dict[str, Any], *, read: Any, matches: Any, label: str
) -> bool:
    """Read the record back after an accepted apply; a failed read is never a success."""
    try:
        observed = read()
    except (OSError, TimeoutError, http.client.HTTPException, ValueError, LaunchplaneSafetyError) as exc:
        diagnostics_source = _error_payload_from_http(exc) if isinstance(exc, urllib.error.HTTPError) else exc
        payload["summary"].update({"read_back_" + key: value
                                   for key, value in service_response_diagnostics(diagnostics_source).items()})
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
    provider_payload = None
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
        if post_attempted and operation in DOKPLOY_COMPOSE_DOMAIN_COMMANDS and exc.code == 400:
            # The service also uses this 400 for provider failures after partial writes.
            try:
                error = summarize_http_error(operation=operation, request=request, exc=exc)
            except LaunchplaneSafetyError:
                emit(_apply_outcome_unknown(operation=operation, request=request, label=label))
                return 1
            error_summary = _require_dict(error["summary"])
            if error_summary.get("error_code") == "invalid_dokploy_target_setup":
                unknown = _apply_outcome_unknown(operation=operation, request=request, label=label)
                _require_dict(unknown["summary"]).update({
                    field: error_summary[field] for field in ("http_status", "trace_id", "error_code")
                })
                emit(unknown)
            else:
                emit(error)
            return 1
        # A gateway error after the POST began may follow a write Launchplane completed.
        if post_attempted and (exc.code >= 500 or exc.code == 408):
            unknown = _apply_outcome_unknown(operation=operation, request=request, label=label)
            error_payload = _error_payload_from_http(exc)
            _require_dict(unknown["summary"]).update(service_response_diagnostics(error_payload))
            if (
                request.get("calendar_operation")
                and _require_dict(unknown["summary"]).get("error_code") == "calendar_qualification_unavailable"
            ):
                _require_dict(unknown["summary"])["recommendation"] = CALENDAR_QUALIFICATION_ADVICE + CALENDAR_RETRY_ADVICE
            if operation.startswith("dokploy-target-"):
                try:
                    error = summarize_http_error(operation=operation, request=request, exc=exc,
                                                 provider_payload=error_payload)
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
            payload = _apply_response_unverified(operation=operation, request=request, label=label)
            cast(dict[str, object], payload["summary"]).update(service_response_diagnostics(provider_payload))
            emit(payload)
            return 1
        emit_safety_error_payload(operation=operation, request=request, exc=exc, provider_payload=exc)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as exc:
        if post_attempted:
            payload = _apply_outcome_unknown(operation=operation, request=request, label=label)
            cast(dict[str, object], payload["summary"]).update(service_response_diagnostics(exc))
            emit(payload)
            return 1
        emit_read_transport_error(operation=operation, request=request, timeout=args.timeout, exc=exc)
        return 1
    except (ValueError, json.JSONDecodeError) as exc:
        if post_attempted:
            payload = _apply_response_unverified(operation=operation, request=request, label=label)
            cast(dict[str, object], payload["summary"]).update(service_response_diagnostics(
                exc if isinstance(exc, ServiceResponseError) else provider_payload))
            emit(payload)
            return 1
        emit_invalid_response(operation=operation, request=request, diagnostics=service_response_diagnostics(exc))
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
    if request.get("calendar_operation"):
        _require_dict(payload["summary"])["recommendation"] += CALENDAR_RETRY_ADVICE
    return payload


def _apply_outcome_unknown(
    *, operation: str, request: dict[str, object], label: str
) -> dict[str, object]:
    payload = base_payload(status="outcome_unknown", operation=operation, request=request)
    payload["summary"] = {
        "recommendation": (
            "The apply outcome is unknown because the exchange failed after the POST began. "
            + (_recovery_apply_readback(request)
               if operation == "generic-web-deploy-recovery-apply"
               else f"Read back the {label} before any retry.")
        )
    }
    payload["warnings"] = [
        warning("apply_outcome_unknown", "Do not retry this apply until read-back resolves it.")
    ]
    if request.get("calendar_operation"):
        _require_dict(payload["summary"])["recommendation"] += CALENDAR_RETRY_ADVICE
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
        target.get("target_id"): target.get("record_id")
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


def execute_dokploy_application_retain(
    *, args: argparse.Namespace, request: dict[str, object], body: dict[str, object]
) -> int:
    expected_custody = cast(str, body["expected_state_volume_custody_sha256"])

    def finish(
        _settings: dict[str, str], provider_payload: dict[str, Any], payload: dict[str, Any]
    ) -> bool:
        # Retention performs no provider effect, so the receipt itself is the evidence:
        # it must name this plan, key and reviewed custody, and nothing became the target.
        receipt = _require_dict(provider_payload.get("result"))
        retained = (
            receipt.get("allocation_state") == "retained_uncertain"
            and receipt.get("applied") is False
            and receipt.get("deployment_performed") is False
            and receipt.get("resources_retained") is True
            and receipt.get("plan_id") == body["state_volume_plan_id"]
            and receipt.get("original_key") == args.idempotency_key.strip()
            and receipt.get("reviewed_custody_sha256") == expected_custody
        )
        result = payload["result"]
        result["reviewed_plan_sha256"] = args.expected_plan_digest.strip().lower()
        if retained:
            result["reviewed_custody_sha256"] = expected_custody
            payload["summary"]["recommendation"] = (
                "Launchplane retained the uncertain allocation's custody; it can never resume. "
                "Keep its resources. To continue, run a fresh dry run and apply it with a new key."
            )
        else:
            payload["warnings"].append(
                warning(
                    "retention_differs_from_review",
                    "Launchplane did not report retaining the reviewed allocation custody "
                    "under the original key.",
                )
            )
        return retained

    return execute_verified_apply(
        args=args,
        operation=args.command,
        request=request,
        path=helper_command_path(args.command),
        body=body,
        preflight=lambda _settings: None,
        finish=finish,
        label="storage allocation",
    )


def execute_dokploy_application_apply(
    *, args: argparse.Namespace, request: dict[str, object], body: dict[str, object]
) -> int:
    context = cast(str, body["context"]).strip()
    expected_plan_digest = args.expected_plan_digest.strip().lower()
    state_volume = request["state_volume"] is True

    def finish(
        settings: dict[str, str], provider_payload: dict[str, Any], payload: dict[str, Any]
    ) -> bool:
        result = payload["result"]
        result["reviewed_plan_sha256"] = expected_plan_digest
        if result.get("allocation_state") == "retained_uncertain":
            # The service kept an uncertain allocation's custody; nothing became the target.
            payload["warnings"].append(
                warning(
                    "state_volume_allocation_retained",
                    "Launchplane retained the earlier uncertain allocation instead of creating "
                    "a target. Keep the original key and plan; a new allocation needs a new dry run.",
                )
            )
            return False
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
        # An adopted receipt recovered from database evidence does not prove the mount.
        mount_verified = not state_volume or (
            result.get("allocation_state") == "adopted"
            and result.get("configuration_evidence") == "matches"
        )
        if not mount_verified:
            payload["warnings"].append(
                warning(
                    "state_volume_configuration_unverified",
                    "Launchplane did not verify the state volume mount for this receipt. "
                    "Read back with the original key and plan before relying on it.",
                )
            )

        def read() -> dict[str, object]:
            public, private = read_dokploy_target(
                settings=settings, context=context, instance="testing", timeout=args.timeout
            )
            # Compared here and never printed: the records name the created application.
            public["target_ids_agree"] = private["target_ids"] == {created_target_id}
            public["configuration_matches_review"] = (
                private["domains"] == frozenset()
                and private["healthcheck_path"] == body.get("healthcheck_path", "")
            )
            return public

        read_back_ok = attach_read_back(
            payload,
            read=read,
            matches=lambda observed: observed["provider_target_record"] == "present"
            and observed["target_type"] == "application"
            and observed["target_ids_agree"] is True
            and observed["configuration_matches_review"] is True,
            label="Dokploy target",
        )
        return applied_as_reviewed and mount_verified and read_back_ok

    return execute_verified_apply(
        args=args,
        operation=args.command,
        request=request,
        path=helper_command_path(args.command),
        body=body,
        preflight=lambda _settings: None,
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
    provider_payload = None
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
        _require_read_fields("production-backup-authority-read", provider_payload)
        status = public_code(provider_payload.get("status"), default="ok")
        payload = base_payload(status=status, operation=operation, request=request)
        try:
            payload["result"] = _project_production_backup_authority_read(
                provider_payload.get("authority")
            )
        except TypeError as exc:
            raise ServiceResponseError(service_response_diagnostics(provider_payload)) from exc
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
        emit_safety_error_payload(operation=operation, request=request, exc=exc, provider_payload=provider_payload)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as exc:
        emit_read_transport_error(
            operation=operation, request=request, timeout=args.timeout, exc=exc,
        )
        return 1
    except (ValueError, json.JSONDecodeError) as exc:
        emit_invalid_response(operation=operation, request=request, diagnostics=service_response_diagnostics(
            exc if isinstance(exc, ServiceResponseError) else provider_payload))
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
    "tls_ca_pem",
}
PRIVATE_HEALTH_ENDPOINT_RECORD_FIELDS = PRIVATE_HEALTH_ENDPOINT_PAYLOAD_FIELDS | {"updated_at"}
PRIVATE_HEALTH_ENDPOINT_RESULT_FIELDS = {"mode", "endpoint_key", "endpoint_status", "record"}
PRIVATE_HEALTH_ENDPOINT_STATUSES = {"active", "disabled"}
PRIVATE_HEALTH_ENDPOINT_PLAN_STATUSES = {"dry-run": "planned", "apply": "applied"}
PRIVATE_HEALTH_ENDPOINT_MAX_RECORDS = 100
PRIVATE_HEALTH_ENDPOINT_MAX_CA_LENGTH = 65536
# Launchplane's typed confirmation for a private health endpoint apply; the reviewed
# dry-run evidence and plan digest are what the helper checks before sending it.
PRIVATE_HEALTH_ENDPOINT_CONFIRMATION = "APPLY LAUNCHPLANE PRIVATE HEALTH ENDPOINT"


def _private_health_endpoint_ca_sha256(source: dict[str, Any]) -> str:
    """Validate public-only testing HTTPS trust; expose only its byte digest."""
    pem = source.get("tls_ca_pem", "")
    if not isinstance(pem, str) or len(pem) > PRIVATE_HEALTH_ENDPOINT_MAX_CA_LENGTH:
        raise ValueError("invalid_private_health_endpoint_tls_ca")
    if not pem:
        return ""
    instance, url = source.get("instance"), source.get("url")
    if (
        not isinstance(instance, str) or instance.strip() != "testing"
        or not isinstance(url, str) or urllib.parse.urlsplit(url.strip()).scheme != "https"
    ):
        raise ValueError("invalid_private_health_endpoint_tls_ca_scope")
    blocks = re.findall(
        r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+?-----END CERTIFICATE-----", pem
    )
    if not blocks or "".join(pem.split()) != "".join("".join(blocks).split()):
        raise ValueError("invalid_private_health_endpoint_tls_ca")
    # Imported only for scoped trust. Other consumers importing the transport
    # retain their own PEP 723 dependency environments.
    try:
        from cryptography import x509
    except ImportError as exc:
        raise ValueError("private_health_endpoint_tls_ca_dependency_unavailable") from exc

    try:
        certificates = x509.load_pem_x509_certificates(pem.encode("ascii"))
        if not certificates or any(
            not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
            for cert in certificates
        ):
            raise ValueError("invalid_private_health_endpoint_tls_ca")
    except (ValueError, x509.ExtensionNotFound, x509.DuplicateExtension) as exc:
        raise ValueError("invalid_private_health_endpoint_tls_ca") from exc
    return hashlib.sha256(pem.encode("ascii")).hexdigest()


def _project_private_health_endpoint_record(value: object) -> dict[str, object]:
    """Key, scope, status and update time; the URL and free-text source label are dropped."""
    source = _require_dict(value)
    if any(str(key) not in PRIVATE_HEALTH_ENDPOINT_RECORD_FIELDS for key in source):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    if source.get("status") not in PRIVATE_HEALTH_ENDPOINT_STATUSES:
        raise LaunchplaneSafetyError("invalid_response")
    try:
        ca_sha256 = _private_health_endpoint_ca_sha256(source)
    except ValueError as exc:
        raise LaunchplaneSafetyError("invalid_response") from exc
    projected: dict[str, object] = {
        "endpoint_key": public_identifier(source.get("endpoint_key")),
        "product": public_identifier(source.get("product")),
        "context": public_identifier(source.get("context")),
        "instance": public_identifier(source.get("instance")),
        "status": source["status"],
        "updated_at": _optional_text(source.get("updated_at")),
    }
    if ca_sha256:
        projected["tls_ca_sha256"] = ca_sha256
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
            **({"tls_ca_sha256": record["tls_ca_sha256"]} if record.get("tls_ca_sha256") else {}),
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
    _private_health_endpoint_ca_sha256(payload)
    return payload


def private_health_endpoint_body(
    args: argparse.Namespace, *, mode: str
) -> tuple[dict[str, object], dict[str, object]]:
    """The route body and the public request summary for a private endpoint payload."""
    payload = private_health_endpoint_payload(args)
    reason = _required_argument(args, "reason")
    # Sorted, so a retry sends the same body however the payload file orders its keys.
    endpoint = {
        field: value.strip() if isinstance(value, str) and field != "tls_ca_pem" else value
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
            or reviewed_record.get("tls_ca_sha256", "")
            != _private_health_endpoint_ca_sha256(endpoint)
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
    try:
        _require_read_fields("private-health-endpoint-record-read", provider_payload)
        record = provider_payload.get("record")
        return _project_private_health_endpoint_record(record), _private_health_endpoint_url(record)
    except (LaunchplaneSafetyError, ValueError, TypeError) as exc:
        raise ServiceResponseError(service_response_diagnostics(provider_payload)) from exc


def execute_private_health_endpoint_apply(
    *, args: argparse.Namespace, request: dict[str, object], body: dict[str, object]
) -> int:
    endpoint = cast(dict[str, Any], body["endpoint"])
    expected_plan_digest = args.expected_plan_digest.strip().lower()
    expected_ca_sha256 = _private_health_endpoint_ca_sha256(endpoint)

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
            and result["record"].get("tls_ca_sha256", "") == expected_ca_sha256
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
            public["tls_ca_matches_review"] = (
                public.get("tls_ca_sha256", "") == expected_ca_sha256
            )
            return public

        read_back_ok = attach_read_back(
            payload,
            read=read,
            matches=lambda observed: observed["url_matches_review"] is True
            and observed["tls_ca_matches_review"] is True
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
    provider_payload = None
    try:
        provider_payload = request_launchplane_read(
            service_url=settings["service_url"],
            path=helper_command_path(operation),
            settings=settings,
            query=query,
            timeout=args.timeout,
        )
        _require_read_fields("private-health-endpoint-read", provider_payload)
        records = provider_payload.get("records")
        if not isinstance(records, list) or len(records) > PRIVATE_HEALTH_ENDPOINT_MAX_RECORDS:
            raise LaunchplaneSafetyError("invalid_response")
        status = public_code(provider_payload.get("status"), default="ok")
        payload = base_payload(status=status, operation=operation, request=request)
        try:
            payload["result"] = {
                "count": len(records),
                "records": [_project_private_health_endpoint_record(record) for record in records],
            }
        except TypeError as exc:
            raise ServiceResponseError(service_response_diagnostics(provider_payload)) from exc
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
        emit_safety_error_payload(operation=operation, request=request, exc=exc, provider_payload=provider_payload)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as exc:
        emit_read_transport_error(
            operation=operation, request=request, timeout=args.timeout, exc=exc,
        )
        return 1
    except (ValueError, json.JSONDecodeError) as exc:
        emit_invalid_response(operation=operation, request=request, diagnostics=service_response_diagnostics(
            exc if isinstance(exc, ServiceResponseError) else provider_payload))
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
    "rollback_status",
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
    "rollback_target_deployment_record_id",
    "rollback_deployment_record_id",
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
    _require_read_fields("product-promotion-status-read", provider_payload)
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
    provider_payload = None
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
        emit_safety_error_payload(operation=operation, request=request, exc=exc, provider_payload=provider_payload)
        return 1
    except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as exc:
        emit_read_transport_error(
            operation=operation, request=request, timeout=args.timeout, exc=exc,
        )
        return 1
    except (ValueError, json.JSONDecodeError) as exc:
        emit_invalid_response(operation=operation, request=request, diagnostics=service_response_diagnostics(
            exc if isinstance(exc, ServiceResponseError) else provider_payload))
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


OPERATION_PATH_CHECK_TIMEOUT_SECONDS = 300.0

def parse_args(argv: list[str]) -> argparse.Namespace:
    from launchplane_backup_credentials import PREFLIGHT, PREFLIGHT_TIMEOUT_SECONDS
    from launchplane_testing_resources import TIMEOUT_SECONDS as testing_resources_timeout
    parser = argparse.ArgumentParser(description="Execute bounded Launchplane operator actions.")
    parser.add_argument("--config", help="Optional private operator JSON config path.")
    parser.add_argument("--env-config", help="Optional private operator .env config path.")
    parser.add_argument("--url", help="Optional Launchplane service URL override.")
    parser.add_argument(
        "--timeout", type=float, default=argparse.SUPPRESS,
        help=(f"HTTP timeout seconds (testing replacement resources: {testing_resources_timeout:g}; "
              "operation path checks: 300; disposable lane collect: 240; promotion status, Dokploy inventory "
              "and other disposable lane commands: 30; controller: 180; other commands: 10)."),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    from launchplane_testing_rebuild import add_parser as add_testing_rebuild_parser
    add_testing_rebuild_parser(subparsers)
    from launchplane_testing_private_ingress import add_parser as add_private_ingress_parser
    add_private_ingress_parser(subparsers)
    from launchplane_disposable_lane import add_parser as add_disposable_lane_parser
    add_disposable_lane_parser(subparsers)
    from launchplane_testing_resources import add_parser as add_testing_resources_parser
    add_testing_resources_parser(subparsers)
    from launchplane_workflow_rule_retirement import add_parser as add_workflow_rule_retirement_parser
    add_workflow_rule_retirement_parser(subparsers)
    from launchplane_runtime_setting_removal import add_parser as add_runtime_setting_removal_parser
    add_runtime_setting_removal_parser(subparsers)

    from launchplane_backup_credentials import add_parser as add_backup_credentials_parser
    add_backup_credentials_parser(subparsers)
    from launchplane_review_withdrawal import add_parser as add_review_withdrawal_parser
    add_review_withdrawal_parser(subparsers)

    subparsers.add_parser(
        "operator-config-diagnostic",
        help="Report private operator credential source presence without printing values.",
    )

    governance_read = subparsers.add_parser("governance-projection-read", help="Read bounded governance evidence for one pull request.")
    governance_read.add_argument("--repository", required=True)
    governance_read.add_argument("--base-branch", required=True)
    governance_read.add_argument("--pr", type=int, required=True)

    policy_read = subparsers.add_parser(
        "merge-train-policy-read",
        help="Read enrollment and enqueue routing for one repository from the active policy.",
    )
    policy_read.add_argument("--repo", help="GitHub OWNER/REPO; otherwise resolve origin.")
    policy_read.add_argument("--repo-root", default=".")

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
    apply.add_argument("--expected-plan-digest", default="")
    apply.add_argument("--dry-run-evidence-file", default="")
    for command in ("product-expected-config-dry-run", "product-expected-config-apply"):
        expected_config = subparsers.add_parser(command, help="Add or remove declared product configuration requirements; never credential values.")
        expected_config.add_argument("--payload-file", required=True, help="Private local JSON metadata file.")
        expected_config.set_defaults(idempotency_key="")
        if command.endswith("-apply"):
            expected_config.add_argument("--idempotency-key", required=True)
            expected_config.add_argument("--reviewed-dry-run", action="store_true")
            expected_config.add_argument("--dry-run-evidence-file", required=True, help="Saved helper output for the exact reviewed metadata.")

    proposal = subparsers.add_parser("privileged-policy-propose", help="Submit an inert policy plan for signed-in Director review; never approve or apply.")
    proposal.add_argument("--payload-file", required=True, help="Private local JSON envelope with a stable source_event_id for retries.")
    proposal.set_defaults(idempotency_key="")
    trusted_proposal = subparsers.add_parser(
        "merge-train-trusted-automation-propose",
        help="Propose adding or removing one trusted automation id on named enrolled repositories; never approve or apply.",
    )
    trusted_proposal.add_argument("--change", choices=("add", "remove"), required=True)
    trusted_proposal.add_argument("--github-user-id", type=int, required=True, help="Immutable GitHub numeric user id.")
    trusted_proposal.add_argument("--repo", action="append", required=True, help="Enrolled OWNER/REPO; repeat per repository.")
    enrollment_proposal = subparsers.add_parser(
        "merge-train-enrollment-propose",
        help="Propose enrolling repository branches with an enrolled repository's settings; never approve or apply.",
    )
    enrollment_proposal.add_argument("--repo", action="append", required=True, help="OWNER/REPO to enroll; repeat per repository.")
    enrollment_proposal.add_argument(
        "--repository-id", type=int, action="append", required=True, help="Immutable GitHub repository id, one per --repo in order."
    )
    enrollment_proposal.add_argument("--base-branch", action="append", help="Branch to enroll, one per --repo in order; default main.")
    enrollment_proposal.add_argument("--like", required=True, help="Enrolled OWNER/REPO whose target settings to copy.")
    enrollment_proposal.add_argument("--like-base-branch", default="main", help="The template's enrolled branch.")
    for merge_train_proposal in (trusted_proposal, enrollment_proposal):
        merge_train_proposal.add_argument("--reason", required=True)
        merge_train_proposal.add_argument("--related-issue", default="")
        merge_train_proposal.add_argument(
            "--source-event-id", required=True, help="Stable id; reuse it to replay an uncertain submission."
        )
        merge_train_proposal.add_argument("--updated-at", default="", help="Reported updated_at, to replay an uncertain submission.")
        merge_train_proposal.add_argument(
            "--expected-active-record-id", default="", help="Reported active_record_id; refuses if the active policy changed since."
        )
        merge_train_proposal.set_defaults(idempotency_key="")

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

    dokploy_inventory_read = subparsers.add_parser("dokploy-inventory-read", help="Read one value-free Dokploy inventory page, including empty/untracked projects.")
    dokploy_inventory_read.add_argument("--offset", type=int, default=0)
    dokploy_inventory_read.add_argument("--limit", type=int, default=25)
    dokploy_inventory_read.add_argument("--inventory-digest", default="")

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

    for command in ("health-monitoring-dry-run", "health-monitoring-apply"):
        monitoring = subparsers.add_parser(command, help="Plan or apply one lane's health monitoring check, preserving its endpoint.")
        for argument in ("--product", "--context", "--instance", "--check-name", "--reason"):
            monitoring.add_argument(argument, required=True)
        monitoring.add_argument("--check-kind", choices=sorted(HEALTH_MONITORING_KINDS), required=True)
        monitoring.add_argument("--monitoring-intent", choices=sorted(HEALTH_MONITORING_INTENTS), required=True)
        monitoring.add_argument("--enabled", action=argparse.BooleanOptionalAction, required=True)
        monitoring.add_argument("--require-runtime-identity", action=argparse.BooleanOptionalAction, required=True)
        monitoring.add_argument("--private-endpoint-key", default="")
        monitoring.add_argument("--dependency-mode", choices=sorted(HEALTH_DEPENDENCY_MODES),
                                help="Set the lane's dependency coverage policy; omit to preserve it.")
        monitoring.add_argument("--dependency-source", choices=sorted(HEALTH_DEPENDENCY_SOURCES),
                                help="Required with --dependency-mode: derived metadata or an explained override.")
        monitoring.add_argument("--dependency", action="append", choices=sorted(HEALTH_DEPENDENCIES),
                                help="Override dependency; repeat, or omit for an explained none.")
        monitoring.add_argument("--dependency-override-reason", default="")
        _add_reviewed_apply_arguments(monitoring, apply=command.endswith("-apply"))

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
    path_check.add_argument("--intent", choices=("provision", "generation", "testing-replacement"), help="Inspect Odoo operation prerequisites instead of only the testing deploy path.")
    path_check.add_argument("--source-mode", choices=("ssh", "container-export"), help="Select the planned copy source; requires --intent.")
    path_check.add_argument("--operation-id", help="Original testing resource preparation or replacement operation; requires --intent.")
    path_check.add_argument("--task-id", help="Exact provision task id (32 lower-case hex characters); requires --intent.")

    product_activity_read = subparsers.add_parser(
        "product-activity-read",
        help="Read a product's recent deployment, promotion and preview activity.",
    )
    product_activity_read.add_argument("--product", required=True)

    protected_artifacts_read = subparsers.add_parser(
        "protected-artifacts-read",
        help="Read bounded protected-artifact entries and sanitized warnings.",
    )
    protected_artifacts_read.add_argument("--product", required=True)
    protected_artifacts_read.add_argument("--context")

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
    reconcile_requests_read.add_argument("--target-key", help="Select an exact target before bounding the output.")

    product_secret_bindings_read = subparsers.add_parser(
        "product-secret-bindings-read",
        help=(
            "Read a product's runtime secret binding metadata (names, scope, lane, class, "
            "sharing reason and version id); never values."
        ),
    )
    product_secret_bindings_read.add_argument("--product", required=True)

    for command in NAMES_ONLY_READS:
        names_read = subparsers.add_parser(command, help="Read names and metadata only; never emit values.")
        names_read.add_argument("--context", required=True)
        if command.startswith("instance-"):
            names_read.add_argument("--instance", required=True)

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
    target_replacement_plan_read.add_argument(
        "--artifact-id",
        help="Plan with this candidate artifact instead of the lane's current one; needs --source-git-ref.",
    )
    target_replacement_plan_read.add_argument(
        "--source-git-ref",
        help="The candidate artifact's source commit; needs --artifact-id.",
    )

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
            "--purpose", choices=sorted(TESTING_HOLD_PURPOSES), default="staff_testing",
            help="Hold purpose; staging lifts do not request deployment reconciliation.",
        )
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
        (
            "service-runtime-setting-dry-run",
            "Dry-run setting a declared non-secret Launchplane service setting.",
        ),
        (
            "service-runtime-setting-apply",
            "Apply a reviewed service setting bound to the saved dry-run digest.",
        ),
    ):
        service_setting = subparsers.add_parser(command, help=help_text)
        service_setting.add_argument("--key", required=True)
        service_setting.add_argument("--value", required=True)
        service_setting.add_argument("--reason", required=True)
        _add_reviewed_apply_arguments(service_setting, apply=command.endswith("-apply"))

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

    for command, help_text in (
        (
            "dokploy-target-create-application-dry-run",
            "Dry-run creating a testing lane's Dokploy application target from a private payload.",
        ),
        (
            "dokploy-target-create-application-apply",
            "Create the reviewed testing application target bound to the saved dry-run digest.",
        ),
    ):
        application_target = subparsers.add_parser(command, help=help_text)
        application_target.add_argument(
            "--payload-file", required=True, help="Private local JSON payload file."
        )
        _add_reviewed_apply_arguments(application_target, apply=command.endswith("-apply"))
    application_readback = subparsers.add_parser(
        DOKPLOY_APPLICATION_READBACK_COMMAND,
        help="Read back an uncertain storage allocation under its original key; never creates or adopts.",
    )
    application_readback.add_argument(
        "--payload-file", required=True, help="The unchanged private JSON payload file."
    )
    application_readback.add_argument(
        "--idempotency-key", required=True, help="The original apply's key."
    )
    application_readback.add_argument("--expected-plan-digest", required=True)
    application_readback.add_argument(
        "--dry-run-evidence-file",
        required=True,
        help="Private saved JSON output from the reviewed dry-run.",
    )
    application_retain = subparsers.add_parser(
        DOKPLOY_APPLICATION_RETAIN_COMMAND,
        help="Retain an uncertain storage allocation's reviewed custody under its original key.",
    )
    application_retain.add_argument(
        "--payload-file", required=True, help="The unchanged private JSON payload file."
    )
    _add_reviewed_apply_arguments(application_retain, apply=True)
    application_retain.add_argument(
        "--readback-evidence-file",
        required=True,
        help="Private saved JSON output from the original-key readback.",
    )
    application_retain.add_argument(
        "--expected-custody-digest",
        required=True,
        help="The readback's custody_sha256.",
    )

    domain_plan = subparsers.add_parser(
        "dokploy-target-repair-domain-authority-dry-run",
        help="Plan domain authority from a fresh tracked-target inspection; never apply.",
    )
    domain_plan.add_argument("--context", required=True)
    domain_plan.add_argument("--instance", required=True)
    domain_plan.add_argument("--reason", required=True)
    ingress_plan = subparsers.add_parser(
        "ingress-route-dry-run",
        help="Verify an existing ingress no-op from private observed route evidence; never apply.",
    )
    ingress_plan.add_argument("--payload-file", required=True)

    repair = subparsers.add_parser("dokploy-target-repair-domain-authority-apply",
        help="Apply only the reviewed exact live-domain authority record repair.")
    for field in ("context", "instance", "reason"):
        repair.add_argument(f"--{field}", required=True)
    _add_reviewed_apply_arguments(repair, apply=True)
    ingress_authority = subparsers.add_parser("ingress-route-authority-apply",
        help="Record an exact existing-ingress no-op; provider changes remain disabled.")
    ingress_authority.add_argument("--payload-file", required=True)
    _add_reviewed_apply_arguments(ingress_authority, apply=True)
    for command in ("route-authority-discover", "route-binding-read", "route-binding-dry-run", "route-binding-apply"):
        binding = subparsers.add_parser(command, help="Read or reconcile service-owned route authority.")
        for field in ("product", "context", "instance"):
            binding.add_argument(f"--{field}", required=True)
        if command in {"route-binding-dry-run", "route-binding-apply"}:
            binding.add_argument("--reason", required=True)
            _add_reviewed_apply_arguments(binding, apply=command.endswith("-apply"))

    for command in sorted(DOKPLOY_COMPOSE_DOMAIN_COMMANDS):
        domain = subparsers.add_parser(command, help="Review or apply tracked compose web host routes.")
        domain.add_argument("--payload-file", required=True, help="Private local JSON payload file.")
        _add_reviewed_apply_arguments(domain, apply=command.endswith("-apply"))

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

    odoo_website_settings_dry_run = subparsers.add_parser(
        "odoo-website-settings-dry-run",
        help="Dry-run a product's pending production website settings intent from a private payload.",
    )
    odoo_website_settings_dry_run.add_argument(
        "--payload-file", required=True, help="Private local JSON payload file."
    )
    odoo_website_settings_dry_run.set_defaults(idempotency_key="")

    odoo_website_settings_apply = subparsers.add_parser(
        "odoo-website-settings-apply",
        help="Record reviewed website settings intent bound to the saved dry-run digest.",
    )
    odoo_website_settings_apply.add_argument(
        "--payload-file", required=True, help="Private local JSON payload file."
    )
    odoo_website_settings_apply.add_argument("--idempotency-key", required=True)
    odoo_website_settings_apply.add_argument("--reviewed-dry-run", action="store_true")
    odoo_website_settings_apply.add_argument("--expected-plan-digest", required=True)
    odoo_website_settings_apply.add_argument(
        "--dry-run-evidence-file",
        required=True,
        help="Private saved JSON output from the reviewed website-settings dry-run.",
    )

    for command in ("live-target-runtime-sync-dry-run", "live-target-runtime-sync-apply"):
        sync = subparsers.add_parser(command, help="Sync managed runtime values without deployment; key names only.")
        for name in ("product", "context", "instance"):
            sync.add_argument(f"--{name}", required=True)
        sync.add_argument("--key", action="append", default=[], help="Select one existing managed runtime key; repeat for an exact selection.")
        sync.add_argument("--idempotency-key", required=command.endswith("-apply"), default="")
        if command.endswith("-apply"):
            sync.add_argument("--reviewed-dry-run", action="store_true")
            sync.add_argument("--expected-plan-digest", required=True)
            sync.add_argument("--dry-run-evidence-file", required=True)

    recovery_reference_read = subparsers.add_parser(
        "generic-web-deploy-recovery-reference-read",
        help="Read the service-owned reference to one exact held testing event deploy.",
    )
    recovery_reference_read.add_argument("--product", required=True)

    recovery_dry_run = subparsers.add_parser(
        "generic-web-deploy-recovery-dry-run",
        help="Submit a private generic-web deploy-recovery dry-run payload.",
    )
    recovery_dry_run.add_argument(
        "--payload-file", required=True, help="Private local JSON payload file."
    )
    recovery_dry_run.add_argument("--idempotency-key", default="", help="Original deploy key for original_deploy payloads; omit for service references.")

    recovery_apply = subparsers.add_parser(
        "generic-web-deploy-recovery-apply",
        help="Submit a reviewed private generic-web deploy-recovery apply payload.",
    )
    recovery_apply.add_argument(
        "--payload-file", required=True, help="Private local JSON payload file."
    )
    recovery_apply.add_argument("--idempotency-key", default="", help="Original deploy key for original_deploy payloads; omit for service references.")
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
    args = parser.parse_args(argv)
    from launchplane_disposable_lane import TIMEOUTS as disposable_lane_timeouts
    args.timeout = getattr(
        args, "timeout",
        disposable_lane_timeouts[args.command] if args.command in disposable_lane_timeouts
        else PREFLIGHT_TIMEOUT_SECONDS if args.command == PREFLIGHT
        else testing_resources_timeout if args.command.startswith("testing-replacement-resources-")
        else OPERATION_PATH_CHECK_TIMEOUT_SECONDS if args.command == "path-check" and args.intent is not None
        else 180.0 if args.command == "merge-train-controller-run-once"
        else 30.0 if args.command in {"product-promotion-status-read", "dokploy-inventory-read"} else 10.0,
    )
    return args


INGRESS_PLAN_ROUTE_FIELDS = {
    "domain_names", "edge_endpoint_key", "certificate_id", "ssl_forced",
    "hsts_enabled", "hsts_subdomains", "trust_forwarded_proto", "http2_support",
    "npmplus_http3_support", "access_list_id", "npmplus_noindex",
    "npmplus_crowdsec_appsec", "npmplus_proxy_request_buffering",
    "npmplus_proxy_response_buffering", "npmplus_upstream_compression",
    "npmplus_fancyindex", "npmplus_x_frame_options", "npmplus_auth_request", "enabled",
}


def _plan_domains(value: object) -> list[str]:
    if (
        not isinstance(value, list) or not 1 <= len(value) <= 100
        or any(not isinstance(host, str) or len(host) > 253 or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", host
        ) for host in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("invalid_plan_domains")
    return value


def _ingress_plan_input(args: argparse.Namespace) -> dict[str, Any]:
    body = read_payload_file(args.payload_file)
    if body.keys() - {"product", "context", "instance", "route", "expected_host_id", "reason"}:
        raise ValueError("unsupported_ingress_plan_field")
    for key in ("product", "context", "instance"):
        body[key] = _plan_identifier(body.get(key), key)
    if not PRODUCT_READ_PATH_SEGMENT_RE.fullmatch(body["product"]):
        raise ValueError("invalid_product")
    if not isinstance(body.get("reason"), str) or not body["reason"].strip():
        raise ValueError("reason_required")
    if type(body.get("expected_host_id")) is not int or body["expected_host_id"] < 1:
        raise ValueError("expected_host_id_required")
    route = body.get("route")
    if not isinstance(route, dict) or route.keys() - INGRESS_PLAN_ROUTE_FIELDS:
        raise ValueError("unsupported_ingress_route_field")
    _plan_domains(route.get("domain_names"))
    route["edge_endpoint_key"] = _plan_identifier(route.get("edge_endpoint_key"), "edge_endpoint_key")
    # All route options have value-only scalar types; no arbitrary provider config.
    for key, value in route.items():
        if key in {"domain_names", "edge_endpoint_key"}:
            continue
        if key in {"certificate_id", "access_list_id"}:
            if type(value) is not int or value < 0:
                raise ValueError("invalid_ingress_route_option")
        elif key == "npmplus_x_frame_options":
            if not isinstance(value, str) or value not in {"DENY", "SAMEORIGIN", "upstream", "none"}:
                raise ValueError("invalid_ingress_route_option")
        elif key == "npmplus_auth_request":
            if not isinstance(value, str) or value not in {"none", "anubis", "tinyauth", "authelia", "authentik", "authentik-send-basic-auth"}:
                raise ValueError("invalid_ingress_route_option")
        elif type(value) is not bool:
            raise ValueError("invalid_ingress_route_option")
    return body


def _plan_identifier(value: object, field: str) -> str:
    try:
        return public_identifier(value)
    except LaunchplaneSafetyError:
        raise ValueError(f"invalid_{field}") from None


def _domain_authority_plan_body(
    args: argparse.Namespace, read: Any,
) -> dict[str, Any]:
    observed = _require_dict(read(
        internal_helper_path("dokploy-target-inspect"),
        {"context": args.context, "instance": args.instance},
    ).get("inspect"))
    tracked = _require_dict(observed.get("tracked_target"))
    provider = _require_dict(observed.get("provider_target_record"))
    live = _require_dict(observed.get("provider"))
    target_id = tracked.get("target_id")
    target_type = tracked.get("target_type")
    if (
        observed.get("status") != "ok" or provider.get("status") != "present"
        or not isinstance(target_type, str) or target_type not in {"application", "compose"}
        or not isinstance(target_id, str) or not target_id
        or any(source.get("target_id") != target_id for source in (observed, provider))
        or live.get("id") != target_id
        or ("target_id" in live and live["target_id"] != target_id)
        or any(source.get("target_type") != target_type for source in (observed, live))
        or tracked.get("context") != args.context or tracked.get("instance") != args.instance
        or provider.get("provider_id") != "dokploy"
        or provider.get("target_category") != target_type
        or provider.get("provider_target_type") != target_type
        or not isinstance(provider.get("display_name"), str) or not provider["display_name"]
    ):
        raise LaunchplaneSafetyError("invalid_response")
    routes = live.get("domains")
    if not isinstance(routes, list) or not 1 <= len(routes) <= 100:
        raise LaunchplaneSafetyError("invalid_response")
    hosts = [_require_dict(route).get("host") for route in routes]
    if any(not isinstance(host, str) for host in hosts):
        raise LaunchplaneSafetyError("invalid_response")
    domains = list(dict.fromkeys(host.strip().lower().rstrip(".").encode("idna").decode("ascii") for host in hosts))
    _plan_domains(domains)
    expected = {key: provider[key] for key in (
        "provider_id", "target_category", "target_id", "display_name", "provider_target_type",
    )}
    # Complete canonical evidence stays private. Noncanonical evidence uses the
    # service's keyed full-reference comparison instead of exporting its values.
    expected["provider_evidence"] = provider.get("provider_evidence", {})
    body = {
        "product": "launchplane", "context": args.context, "instance": args.instance,
        "operation": "repair-domain-authority", "mode": "dry-run",
        "target_type": target_type, "target_id": target_id, "domains": domains,
        "expected_current_provider_target": expected, "reason": args.reason,
    }
    if "comparison_fingerprint" in provider:
        fingerprint = provider["comparison_fingerprint"]
        if not isinstance(fingerprint, str) or not 1 <= len(fingerprint) <= 256:
            raise LaunchplaneSafetyError("invalid_response")
        body["expected_current_provider_target_fingerprint"] = fingerprint
    return body


def _ingress_plan_body(body: dict[str, Any], read: Any) -> dict[str, Any]:
    profile = _require_dict(read(
        _product_read_path("product-profile-read", product=body["product"]), {},
    ).get("profile"))
    lanes = profile.get("lanes")
    if profile.get("product") != body["product"] or not isinstance(lanes, list):
        raise LaunchplaneSafetyError("invalid_response")
    matches = [lane for lane in lanes if isinstance(lane, dict)
               and lane.get("context") == body["context"] and lane.get("instance") == body["instance"]]
    if len(matches) != 1:
        raise ValueError("plan_lane_not_unique")
    lane = matches[0]
    urls = [lane.get("base_url"), lane.get("health_url")]
    monitoring = lane.get("health_monitoring") or {}
    checks = _require_dict(monitoring).get("checks", [])
    if not isinstance(checks, list):
        raise LaunchplaneSafetyError("invalid_response")
    for check in checks:
        if isinstance(check, dict) and check.get("enabled") is True and check.get("kind") == "public_http":
            urls.append(check.get("url"))
    lane_domains = set()
    for value in urls:
        if not value:
            continue
        if not isinstance(value, str):
            raise LaunchplaneSafetyError("invalid_response")
        url = urllib.parse.urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.port not in {None, 443}:
            raise ValueError("invalid_plan_lane_url")
        lane_domains.add(url.hostname.rstrip(".").lower())
    if set(body["route"]["domain_names"]) != lane_domains:
        raise ValueError("plan_domain_scope_mismatch")
    endpoint_key = body["route"]["edge_endpoint_key"]
    endpoint = _require_dict(read(internal_helper_path("ingress-plan-edge-endpoint-read").format(
        endpoint_key=urllib.parse.quote(endpoint_key, safe="")), {}).get("record"))
    if endpoint.get("endpoint_key") != endpoint_key or endpoint.get("status") != "active":
        raise ValueError("plan_endpoint_not_active")
    return {"product": body["product"], "context": body["context"], "instance": body["instance"],
            "ingress": {"mode": "dry-run", "route": body["route"],
                        "expected_host_id": body["expected_host_id"],
                        "require_exact_expected_host_domains": True,
                        "allow_create": False, "allow_update": False, "allow_enable_disable": False,
                        "reason": body["reason"]}}


def _project_inert_plan(provider: dict[str, Any], body: dict[str, Any]) -> dict[str, object]:
    if provider.keys() - SUCCESS_TOP_LEVEL_KEYS or provider.get("status") != "accepted":
        raise LaunchplaneSafetyError("invalid_response")
    if "replayed" in provider and type(provider["replayed"]) is not bool:
        raise LaunchplaneSafetyError("invalid_response")
    if provider.get("original_trace_id"):
        public_trace_id(provider["original_trace_id"])
    result = _require_dict(provider.get("result"))
    if "ingress" not in body:
        _project_records(provider.get("records"), set())
        if result.keys() - {"mode", "operation", "context", "instance", "applied", "route_domain_ids", "setup", "reason"}:
            raise LaunchplaneSafetyError("unsafe_response_shape")
        if (result.get("mode") != "dry-run" or result.get("applied") is not False
            or result.get("operation") != body["operation"]
            or result.get("context") != body["context"] or result.get("instance") != body["instance"]):
            raise LaunchplaneSafetyError("invalid_response")
        setup = _require_dict(result.get("setup"))
        if setup.keys() - {"applied", "target_record", "target_id_record", "provider_target_record", "requested_domains", "live_provider_domains", "warnings"}:
            raise LaunchplaneSafetyError("unsafe_response_shape")
        expected_provider = dict(body["expected_current_provider_target"])
        if body.get("expected_current_provider_target_fingerprint"):
            expected_provider.pop("provider_evidence", None)
            expected_provider["comparison_fingerprint"] = body["expected_current_provider_target_fingerprint"]
            if "provider_evidence" in _require_dict(setup.get("provider_target_record")):
                raise LaunchplaneSafetyError("invalid_response")
        if (setup.get("applied") is not False
            or set(_plan_domains(setup.get("requested_domains"))) != set(body["domains"])
            or set(_plan_domains(setup.get("live_provider_domains"))) != set(body["domains"])
            or _require_dict(setup.get("target_id_record")).get("target_id") != body["target_id"]
            or _require_dict(setup.get("target_record")).get("target_type") != body["target_type"]
            or set(_plan_domains(_require_dict(setup.get("target_record")).get("domains"))) != set(body["domains"])
            or any(_require_dict(setup.get("provider_target_record")).get(key) != value
                   for key, value in expected_provider.items())):
            raise LaunchplaneSafetyError("invalid_response")
        return {"mode": "dry-run", "applied": False, "domain_count": len(body["domains"]),
                "exact_domains_verified": True, "target_identity_verified": True}
    if result.keys() - {"status", "dry_run", "operations", "proxy_host"}:
        raise LaunchplaneSafetyError("unsafe_response_shape")
    if result.get("dry_run") is not True or result.get("status") != "unchanged":
        raise LaunchplaneSafetyError("invalid_response")
    host = _require_dict(result.get("proxy_host"))
    ingress = body["ingress"]
    domains = set(ingress["route"]["domain_names"])
    records = _require_dict(provider.get("records"))
    operations = result.get("operations")
    if (records.keys() - {"ingress_provider", "ingress_provider_domain_comparison", "ingress_provider_domain_count", "ingress_route_audit_record_id"}
        or type(host.get("id")) is not int or host.get("id") != ingress["expected_host_id"]
        or set(_plan_domains(host.get("domain_names"))) != domains
        or records.get("ingress_provider_domain_comparison") != "requested_domains"
        or type(records.get("ingress_provider_domain_count")) is not int
        or records.get("ingress_provider_domain_count") != len(domains)
        or not isinstance(operations, list) or not 1 <= len(operations) <= 100):
        raise LaunchplaneSafetyError("invalid_response")
    for operation in operations:
        operation = _require_dict(operation)
        if (operation.keys() - {"action", "host_id", "domain_names", "requires_apply", "change_categories"}
            or operation.get("requires_apply") is not False or operation.get("action") != "no-op"
            or operation.get("change_categories", []) != []
            or operation.get("host_id") != ingress["expected_host_id"]
            or set(_plan_domains(operation.get("domain_names"))) != domains):
            raise LaunchplaneSafetyError("invalid_response")
    return {"mode": "dry-run", "dry_run": True, "status": "unchanged",
            "domain_count": len(domains), "operation_count": len(operations),
            "exact_domains_verified": True, "requires_apply": False}


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        from launchplane_testing_rebuild import COMMANDS as testing_rebuild_commands, execute as execute_testing_rebuild
        if args.command in testing_rebuild_commands:
            return execute_testing_rebuild(sys.modules[__name__], args)
        from launchplane_testing_private_ingress import COMMANDS as private_ingress_commands, execute as execute_private_ingress
        if args.command in private_ingress_commands:
            return execute_private_ingress(sys.modules[__name__], args)
        from launchplane_disposable_lane import COMMANDS as disposable_lane_commands, execute as execute_disposable_lane
        if args.command in disposable_lane_commands:
            return execute_disposable_lane(sys.modules[__name__], args)
        from launchplane_testing_resources import COMMANDS as testing_resources_commands, execute as execute_testing_resources
        if args.command in testing_resources_commands:
            return execute_testing_resources(sys.modules[__name__], args)
        from launchplane_workflow_rule_retirement import (
            COMMANDS as workflow_rule_retirement_commands, execute as execute_workflow_rule_retirement,
        )
        if args.command in workflow_rule_retirement_commands:
            return execute_workflow_rule_retirement(sys.modules[__name__], args)
        from launchplane_runtime_setting_removal import (
            COMMANDS as runtime_setting_removal_commands, execute as execute_runtime_setting_removal,
        )
        if args.command in runtime_setting_removal_commands:
            return execute_runtime_setting_removal(sys.modules[__name__], args)
        from launchplane_backup_credentials import COMMANDS as backup_credentials_commands, execute as execute_backup_credentials
        if args.command in backup_credentials_commands:
            return execute_backup_credentials(sys.modules[__name__], args)
        from launchplane_review_withdrawal import COMMANDS as review_withdrawal_commands, execute as execute_review_withdrawal
        if args.command in review_withdrawal_commands:
            return execute_review_withdrawal(sys.modules[__name__], args)
        if args.command in NAMES_ONLY_READS:
            context = _request_identifier(args.context)
            instance = _request_identifier(args.instance) if args.command.startswith("instance-") else ""
            request = {"operation": args.command, "context": context, "instance": instance}
            path = helper_command_path(args.command).format(
                context=urllib.parse.quote(context, safe=""),
                instance=urllib.parse.quote(instance, safe=""),
            )
            return execute_product_read(args=args, operation=args.command, request=request, path=path)
        from launchplane_route_authority import COMMANDS as route_authority_commands, execute as execute_route_authority
        if args.command in route_authority_commands:
            return execute_route_authority(sys.modules[__name__], args)
        if args.command == "governance-projection-read":
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository) or args.pr < 1:
                raise ValueError("invalid_request")
            request = {"repository": _request_identifier(args.repository), "base_branch": _request_identifier(args.base_branch), "pull_request_number": args.pr}
            return execute_product_read(
                args=args, operation=args.command, request=request, path=helper_command_path(args.command),
                query={key: str(value) for key, value in request.items()},
            )
        if args.command == "merge-train-policy-read":
            try:
                if args.repo:
                    repository = args.repo.removesuffix(".git")
                else:
                    github_scripts = Path(__file__).resolve().parents[2] / "github" / "scripts"
                    sys.path.insert(0, str(github_scripts))
                    from github_read import resolve_repo

                    repository = resolve_repo(Path(args.repo_root))
                if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
                    raise ValueError("repository_unresolved")
            except (ValueError, ImportError) as exc:
                raise ValueError("repository_unresolved") from exc
            request = {"repository": public_identifier(repository)}
            return execute_product_read(
                args=args, operation=args.command, request=request,
                path=helper_command_path(args.command),
            )
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
        if args.command in MERGE_TRAIN_PROPOSALS:
            return execute_merge_train_proposal(args)
        if args.command == "privileged-policy-propose":
            body = privileged_policy_proposal_body(args)
            request = {"mode": "propose", "payload_source": "private_file", "descriptor_id": body["descriptor_id"]}
            if body["descriptor_id"] == "production-backup-policy":
                authority = _require_dict(_require_dict(body["request"]).get("authority"))
                product = _require_dict(authority.get("policy")).get("product")
                request["product"] = product
            return execute_post(args=args, operation=args.command, path=helper_command_path(args.command), request=request, body=body)
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
        if args.command in {"product-config-dry-run", "product-config-apply"}:
            mode = "apply" if args.command.endswith("-apply") else "dry-run"
            body = product_config_payload_body(args, mode=mode)
            request = {"mode": mode, "payload_source": "private_file"}
            if "odoo_business_hours" in body:
                request = _calendar_request(body)
                if mode == "apply":
                    return _execute_calendar_apply(args, body, request)
            return execute_post(args=args, operation=args.command, path=helper_command_path(args.command), request=request, body=body)
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
        if args.command == "dokploy-inventory-read":
            if not 0 <= args.offset <= 2000 or not 1 <= args.limit <= 100 or (args.offset and not args.inventory_digest) or (args.inventory_digest and not re.fullmatch(r"[a-f0-9]{64}", args.inventory_digest)):
                raise ValueError("invalid_inventory_page")
            return execute_dokploy_inventory_read(args=args)
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
                "product": _request_identifier(args.product),
                "context": _request_identifier(args.context),
                "instance": _request_identifier(args.instance),
                "payload_source": "operator_argument",
            }
            return execute_lane_config_read(args=args, operation=args.command, request=request)
        if args.command == "testing-hold-read":
            request = {
                "product": _request_identifier(args.product),
                "context": _request_identifier(args.context),
                "instance": _request_identifier(args.instance),
                "payload_source": "operator_argument",
            }
            return execute_lane_config_read(args=args, operation=args.command, request=request)
        if args.command == "product-environment-read":
            path = _product_read_path(
                args.command, product=args.product, environment=args.environment
            )
            request = {
                "product": _request_identifier(args.product),
                "environment": _request_identifier(args.environment),
                "payload_source": "operator_argument",
            }
            return execute_product_read(
                args=args, operation=args.command, request=request, path=path
            )
        if args.command == "path-check":
            path = _product_read_path(args.command, product=args.product)
            request = {
                "product": _request_identifier(args.product),
                "path": args.path,
                "payload_source": "operator_argument",
            }
            query = {"path": args.path}
            if any(value is not None for value in (args.intent, args.source_mode, args.operation_id, args.task_id)):
                if args.path != "testing" or args.intent is None:
                    raise LaunchplaneSafetyError("invalid_request")
                query["intent"] = args.intent
                request["intent"] = args.intent
                if args.source_mode is not None:
                    query["source_mode"] = args.source_mode
                if args.operation_id is not None:
                    query["operation_id"] = _request_identifier(args.operation_id)
                if args.task_id is not None:
                    if re.fullmatch(r"[a-f0-9]{32}", args.task_id) is None:
                        raise LaunchplaneSafetyError("invalid_request")
                    query["task_id"] = args.task_id
                request.update({key: value for key, value in query.items() if key != "path"})
            return execute_product_read(
                args=args, operation=args.command, request=request, path=path, query=query,
            )
        if args.command == "product-profile-read":
            path = _product_read_path(args.command, product=args.product)
            request = {
                "product": _request_identifier(args.product),
                "payload_source": "operator_argument",
            }
            return execute_product_read(
                args=args, operation=args.command, request=request, path=path
            )
        if args.command == "protected-artifacts-read":
            query = {"product": _request_identifier(args.product)}
            if args.context is not None:
                query["context"] = _request_identifier(args.context)
            request = {**query, "payload_source": "operator_argument"}
            return execute_product_read(
                args=args, operation=args.command, request=request,
                path=helper_command_path(args.command), query=query,
            )
        if args.command == "product-activity-read":
            path = _product_read_path(args.command, product=args.product)
            request = {
                "product": _request_identifier(args.product),
                "payload_source": "operator_argument",
            }
            return execute_product_read(
                args=args, operation=args.command, request=request, path=path
            )
        if args.command in {"reconcile-requests-read", "product-secret-bindings-read", "generic-web-deploy-recovery-reference-read"}:
            path = _product_read_path(args.command, product=args.product)
            request = {
                "product": _request_identifier(args.product),
                "payload_source": "operator_argument",
            }
            if args.command == "reconcile-requests-read" and args.target_key is not None:
                request["target_key"] = _request_identifier(args.target_key)
            return execute_product_read(
                args=args, operation=args.command, request=request, path=path
            )
        if args.command == "target-replacement-plan-read":
            return execute_target_replacement_plan_read(args=args)
        if args.command == "target-replacement-operation-read":
            path = _product_read_path(args.command, operation_id=args.operation_id)
            request = {
                "operation_id": _request_identifier(args.operation_id),
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
        if args.command in {"health-monitoring-dry-run", "health-monitoring-apply"}:
            mode = "apply" if args.command.endswith("-apply") else "dry-run"
            body, request = health_monitoring_body(args, mode=mode)
            if mode == "apply":
                return execute_health_monitoring_apply(args=args, body=body, request=request)
            return execute_post(
                args=args, operation=args.command, path=helper_command_path(args.command),
                request=request, body=body,
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
                "purpose": body.get("purpose", "staff_testing"),
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
        if args.command in {"service-runtime-setting-dry-run", "service-runtime-setting-apply"}:
            mode = "apply" if args.command == "service-runtime-setting-apply" else "dry-run"
            body = service_runtime_setting_body(args, mode=mode)
            request = {
                "mode": mode,
                "key": body["key"],
                "value_sha256": _service_runtime_setting_value_sha256(args.value),
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
                "product": _request_identifier(args.product),
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
                "product": _request_identifier(args.product),
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
                "product": _request_identifier(args.product),
                "context": _request_identifier(args.context),
                "instance": _request_identifier(args.instance),
                "promotion_action": _request_identifier(args.promotion_action),
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
        if args.command in DOKPLOY_COMPOSE_DOMAIN_COMMANDS:
            mode = "apply" if args.command.endswith("-apply") else "dry-run"
            body, request = compose_domain_body(args, mode=mode)
            if mode == "apply":
                return execute_compose_domain_apply(args=args, request=request, body=body)
            return execute_post(
                args=args, operation=args.command, path=helper_command_path(args.command),
                request=request, body=body,
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
        if args.command == DOKPLOY_APPLICATION_READBACK_COMMAND:
            body, request = dokploy_application_body(args, mode="readback")
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command == DOKPLOY_APPLICATION_RETAIN_COMMAND:
            body, request = dokploy_application_retain_body(args)
            return execute_dokploy_application_retain(args=args, request=request, body=body)
        if args.command in DOKPLOY_APPLICATION_COMMANDS:
            mode = "apply" if args.command.endswith("-apply") else "dry-run"
            body, request = dokploy_application_body(args, mode=mode)
            if mode == "apply":
                return execute_dokploy_application_apply(args=args, request=request, body=body)
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command == "private-health-endpoint-read":
            request = {
                "product": _request_identifier(args.product),
                "context": _request_identifier(args.context),
                "payload_source": "operator_argument",
            }
            if args.instance:
                request["instance"] = _request_identifier(args.instance)
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
                "product": _request_identifier(args.product),
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
                "product": _request_identifier(args.product),
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
        if args.command in {"odoo-website-settings-dry-run", "odoo-website-settings-apply"}:
            mode = "apply" if args.command == "odoo-website-settings-apply" else "dry-run"
            body = odoo_website_settings_body(args, mode=mode)
            request = {
                "mode": mode,
                "product": public_identifier(body["product"]),
                "context": public_identifier(body["context"]),
                "instance": "prod",
                "withdraw": body["website_settings"] is None,
                "payload_source": "private_file",
            }
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command in {"live-target-runtime-sync-dry-run", "live-target-runtime-sync-apply"}:
            return execute_runtime_sync(args)
        if args.command == "generic-web-deploy-recovery-dry-run":
            body = generic_web_deploy_recovery_body(args, mode="dry_run")
            request = _recovery_request(body, mode="dry_run")
            return execute_post(
                args=args,
                operation=args.command,
                path=helper_command_path(args.command),
                request=request,
                body=body,
            )
        if args.command == "generic-web-deploy-recovery-apply":
            body = generic_web_deploy_recovery_body(args, mode="apply")
            request = _recovery_request(body, mode="apply")
            request.update({
                "expected_recovery_digest": body["expected_recovery_digest"],
                "reviewed_recovery_action": args.reviewed_recovery_action,
            })
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
