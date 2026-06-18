#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Execute bounded Launchplane write actions with public-safe output."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


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
TOKEN_PATTERNS = (
    re.compile(r"ghp_[A-Za-z0-9_]+"),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"sk-[A-Za-z0-9_-]+"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
)
DENIED_KEYS = {
    "authorization",
    "cookie",
    "cookies",
    "headers",
    "request",
    "request_body",
    "raw_request",
    "payload",
    "value",
    "values",
    "plaintext",
    "plaintext_value",
    "ciphertext",
    "provider_environment",
    "github_api_base_url",
}
DENIED_KEY_FRAGMENTS = ("token", "password", "master_key")
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
    return {"code": code, "message": message}


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


def resolve_settings(args: argparse.Namespace) -> dict[str, str]:
    config = load_config(args.config)
    env_config = load_operator_env(args.env_config) if args.env_config else {} if args.config else load_operator_env(None)
    if args.config:
        service_url = (args.url or config.get("service_url") or os.environ.get("LAUNCHPLANE_OPERATOR_URL") or "").strip()
    else:
        service_url = (
            args.url
            or os.environ.get("LAUNCHPLANE_OPERATOR_URL")
            or env_config.get("LAUNCHPLANE_OPERATOR_URL")
            or config.get("service_url")
            or ""
        ).strip()
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
    env_config = load_operator_env(args.env_config) if args.env_config else {} if args.config else load_operator_env(None)
    token_env = (config.get("operator_token_env") or "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN").strip()
    subject_env = (
        config.get("operator_subject_env") or "LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT"
    ).strip()
    token_label_env = (
        config.get("operator_token_label_env") or "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL"
    ).strip()
    service_url_candidates = (
        (
            ("argument", bool(args.url)),
            ("json_config", bool(config.get("service_url"))),
            ("environment", bool(os.environ.get("LAUNCHPLANE_OPERATOR_URL"))),
        )
        if args.config
        else (
            ("argument", bool(args.url)),
            ("environment", bool(os.environ.get("LAUNCHPLANE_OPERATOR_URL"))),
            ("private_env", bool(env_config.get("LAUNCHPLANE_OPERATOR_URL"))),
            ("json_config", bool(config.get("service_url"))),
        )
    )
    service_url_sources = [source for source, present in service_url_candidates if present]
    public_hint_sources = public_url_hint_sources(env_config)
    token_present = bool(os.environ.get(token_env) or env_config.get(token_env))
    classification = classify_operator_config(
        service_url_present=bool(service_url_sources),
        token_present=token_present,
        public_url_hint_present=bool(public_hint_sources),
    )
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


def _redact_token_like(value: str) -> str:
    redacted = value
    for pattern in TOKEN_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


def _is_denied_key(key: str) -> bool:
    normalized = key.strip().lower()
    if normalized in DENIED_KEYS:
        return True
    return any(fragment in normalized for fragment in DENIED_KEY_FRAGMENTS)


def sanitize(value: Any) -> object:
    if isinstance(value, dict):
        sanitized: dict[str, object] = {}
        for raw_key, raw_item in value.items():
            key = str(raw_key)
            if _is_denied_key(key):
                continue
            sanitized[key] = sanitize(raw_item)
        return sanitized
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return _redact_token_like(value)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)


def build_url(service_url: str, path: str) -> str:
    return f"{service_url.rstrip('/')}{path}"


def request_launchplane(
    *,
    service_url: str,
    path: str,
    settings: dict[str, str],
    body: dict[str, object],
    timeout: float,
    idempotency_key: str = "",
) -> dict[str, Any]:
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(build_url(service_url, path), data=data, method="POST")
    request.add_header("Accept", "application/json")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {settings['token']}")
    if idempotency_key:
        request.add_header("Idempotency-Key", idempotency_key)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid_response")
    return payload


def _error_payload_from_http(exc: urllib.error.HTTPError) -> dict[str, object]:
    try:
        raw = exc.read().decode("utf-8")
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
    if code == 409 or error_code in {"stale", "mismatched_intent", "matching_dry_run_required"}:
        return "stale"
    return "unavailable"


def http_error_recommendation(status: str) -> str:
    recommendations = {
        "unauthorized": "Credential was not accepted; check the operator token source before retrying.",
        "denied": (
            "Credential was accepted but this action was denied; check the intended "
            "Launchplane authz reconciliation or GitHub Actions OIDC path before probing routes manually."
        ),
        "stale": "Refresh the dry-run or intent evidence before retrying this write action.",
        "unavailable": "Launchplane service was unavailable or returned an invalid error envelope; retry later with trace evidence.",
    }
    return recommendations.get(status, "Stop and surface the compact Launchplane error.")


def summarize_success(
    *, operation: str, request: dict[str, object], provider_payload: dict[str, Any]
) -> dict[str, object]:
    result = provider_payload.get("result")
    sanitized_result = sanitize(result) if isinstance(result, dict) else {}
    records = provider_payload.get("records")
    sanitized_records = sanitize(records) if isinstance(records, dict) else {}
    status = str(provider_payload.get("status") or "accepted")
    payload = base_payload(status=status, operation=operation, request=request)
    payload["records"] = sanitized_records if isinstance(sanitized_records, dict) else {}
    payload["result"] = sanitized_result if isinstance(sanitized_result, dict) else {}

    summary: dict[str, object] = {
        "launchplane_status": status,
        "trace_id": str(provider_payload.get("trace_id") or ""),
        "recommendation": "Review the redacted result before deciding the next action.",
    }
    if isinstance(sanitized_result, dict):
        controller_action = sanitized_result.get("controller_action")
        if isinstance(controller_action, str) and controller_action:
            summary["controller_action"] = controller_action
            summary["recommendation"] = (
                "Stop and report this merge-train state."
                if controller_action in ATTENTION_CONTROLLER_ACTIONS
                else "Call the controller again only after reading this action."
            )
        intent = sanitized_result.get("intent")
        if isinstance(intent, dict):
            summary["intent_status"] = intent.get("status")
            summary["reason_code"] = intent.get("reason_code")
            summary["safe_to_execute"] = intent.get("safe_to_execute")
            summary["recommendation"] = str(
                intent.get("next_action") or summary["recommendation"]
            )
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
        "trace_id": str(provider_payload.get("trace_id") or ""),
        "error_code": str(error.get("code") or status),
        "recommendation": http_error_recommendation(status),
    }
    payload["warnings"] = [
        warning(
            str(error.get("code") or status),
            "Launchplane write action was rejected; inspect the trace in an approved operator surface.",
        )
    ]
    return payload


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
    raw = json.loads(payload_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("invalid_payload")
    return raw


def _require_idempotency(args: argparse.Namespace) -> None:
    if not args.idempotency_key.strip():
        raise ValueError("idempotency_key_required")


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
        if not str(body.get("reason") or "").strip():
            raise ValueError("reason_required")
    return body


def merge_train_controller_body(args: argparse.Namespace) -> dict[str, object]:
    if args.mutate:
        _require_idempotency(args)
    return {
        "schema_version": 1,
        "repository": args.repo,
        "base_branch": args.base_branch,
        "mutate": bool(args.mutate),
    }


def execute_post(
    *,
    args: argparse.Namespace,
    operation: str,
    path: str,
    request: dict[str, object],
    body: dict[str, object],
) -> int:
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
        return 2
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
        emit(summarize_success(operation=operation, request=request, provider_payload=provider_payload))
        return 0
    except urllib.error.HTTPError as exc:
        emit(summarize_http_error(operation=operation, request=request, exc=exc))
        return 1
    except (OSError, TimeoutError, urllib.error.URLError):
        emit(
            unavailable_payload(
                operation=operation,
                request=request,
                status="unavailable",
                code="provider_unavailable",
                message="Launchplane service is unavailable.",
            )
        )
        return 1
    except (ValueError, json.JSONDecodeError):
        emit(
            unavailable_payload(
                operation=operation,
                request=request,
                status="invalid",
                code="invalid_response",
                message="Launchplane returned an invalid response.",
            )
        )
        return 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute bounded Launchplane write actions.")
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

    controller = subparsers.add_parser(
        "merge-train-controller-run-once", help="Call the merge-train controller once."
    )
    controller.add_argument("--repo", required=True, help="Repository in OWNER/REPO form.")
    controller.add_argument("--base-branch", default="main")
    controller.add_argument("--mutate", action="store_true")
    controller.add_argument("--idempotency-key", default="")
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
                path="/v1/agent/write-intents/evaluate",
                request=request,
                body=body,
            )
        if args.command == "product-config-dry-run":
            request = {"mode": "dry-run", "payload_source": "private_file"}
            body = product_config_payload_body(args, mode="dry-run")
            return execute_post(
                args=args,
                operation=args.command,
                path="/v1/product-config/apply",
                request=request,
                body=body,
            )
        if args.command == "product-config-apply":
            request = {"mode": "apply", "payload_source": "private_file"}
            body = product_config_payload_body(args, mode="apply")
            return execute_post(
                args=args,
                operation=args.command,
                path="/v1/product-config/apply",
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
                path="/v1/work-graph/merge-train/controller/run-once",
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
