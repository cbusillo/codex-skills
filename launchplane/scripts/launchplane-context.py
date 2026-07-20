#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Emit optional Launchplane context for public-safe skills."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
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
SAFE_ERROR = "Launchplane context unavailable; continuing without it."


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def base_payload(*, status: str, request: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "provider": PROVIDER,
        "generated_at": utc_now(),
        "request": request,
        "summary": {},
        "sections": {},
        "links": [],
        "warnings": [],
    }


def warning(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def emit(payload: dict[str, object]) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def no_context_payload(request: dict[str, object], *, code: str = "missing_config") -> dict[str, object]:
    payload = base_payload(status="no_context", request=request)
    payload["summary"] = {"recommendation": "Continue without Launchplane context."}
    payload["warnings"] = [warning(code, "Launchplane context is not configured.")]
    return payload


def unavailable_payload(request: dict[str, object], *, status: str, code: str) -> dict[str, object]:
    payload = base_payload(status=status, request=request)
    payload["summary"] = {"recommendation": "Continue without Launchplane context."}
    payload["warnings"] = [warning(code, SAFE_ERROR)]
    return payload


def normalized_request(args: argparse.Namespace) -> dict[str, object]:
    request: dict[str, object] = {"repository": args.repo}
    if args.branch:
        request["branch"] = args.branch
    if args.issue is not None:
        request["issue_number"] = args.issue
    if args.pr is not None:
        request["pr_number"] = args.pr
    return request


def load_config(path: str | None) -> dict[str, str]:
    config: dict[str, str] = {}
    if not path:
        return config
    config_path = Path(path).expanduser()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config
    except (OSError, json.JSONDecodeError):
        raise ValueError("invalid_config") from None
    if not isinstance(raw, dict):
        raise ValueError("invalid_config")
    for key in ("service_url", "token_env", "subject_env", "token_label_env"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            config[key] = value.strip()
    return config


def resolve_settings(args: argparse.Namespace) -> dict[str, str]:
    config = load_config(args.config)
    service_url = (
        args.url
        or os.environ.get("LAUNCHPLANE_CONTEXT_URL")
        or config.get("service_url")
        or ""
    ).strip()
    token_env = (config.get("token_env") or "LAUNCHPLANE_CONTEXT_TOKEN").strip()
    subject_env = (config.get("subject_env") or "LAUNCHPLANE_CONTEXT_SUBJECT").strip()
    token_label_env = (config.get("token_label_env") or "LAUNCHPLANE_CONTEXT_TOKEN_LABEL").strip()
    token = os.environ.get(token_env, "").strip()
    subject = os.environ.get(subject_env, "").strip()
    token_label = os.environ.get(token_label_env, "").strip()
    return {
        "service_url": service_url,
        "token": token,
        "subject": subject,
        "token_label": token_label,
    }


def build_context_url(service_url: str, args: argparse.Namespace) -> str:
    query: dict[str, str] = {}
    if args.repo:
        query["repository"] = args.repo
    if args.branch:
        query["branch"] = args.branch
    if args.issue is not None:
        query["issue_number"] = str(args.issue)
    if args.pr is not None:
        query["pr_number"] = str(args.pr)
    encoded = urllib.parse.urlencode(query)
    return build_launchplane_url(service_url, "/v1/agent/context", query=encoded)


def request_launchplane(url: str, settings: dict[str, str], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/json")
    request.add_header("Authorization", f"Bearer {settings['token']}")
    if settings.get("subject"):
        request.add_header("X-Launchplane-Agent-Subject", settings["subject"])
    if settings.get("token_label"):
        request.add_header("X-Launchplane-Agent-Token-Label", settings["token_label"])
    with safe_urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid_response")
    return payload


def _require_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LaunchplaneSafetyError("invalid_response")
    return value


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise LaunchplaneSafetyError("invalid_response")


def _project_work_graph_item(item: object) -> dict[str, object]:
    source = _require_dict(item)
    projected: dict[str, object] = {}
    source_url = source.get("source_of_truth_url") or source.get("url")
    if source_url:
        projected["source_of_truth_url"] = public_url(source_url)
    if "state" in source:
        projected["state"] = public_code(source["state"])
    safe_to_start = _optional_bool(source.get("safe_to_start"))
    if safe_to_start is not None:
        projected["safe_to_start"] = safe_to_start
    if "next_action" in source:
        projected["next_action"] = public_summary_string(source["next_action"])
    if "why_now" in source:
        projected["why_now"] = public_summary_string(source["why_now"])
    return projected


def _project_repo_mapping_item(item: object) -> dict[str, object]:
    source = _require_dict(item)
    projected: dict[str, object] = {}
    for key in ("repository", "classification", "driver_id"):
        if key in source:
            projected[key] = public_identifier(source[key])
    product_key = source.get("product_key") or source.get("product")
    if product_key:
        projected["product_key"] = public_identifier(product_key)
    if "source_url" in source:
        projected["source_url"] = public_url(source["source_url"])
    return projected


def _project_every_code_request(item: object) -> dict[str, object]:
    source = _require_dict(item)
    projected: dict[str, object] = {}
    if "state" in source:
        projected["state"] = public_code(source["state"])
    source_issue_url = source.get("source_issue_url") or source.get("issue_url")
    if source_issue_url:
        projected["source_issue_url"] = public_url(source_issue_url)
    if source.get("result_pr_url"):
        projected["result_pr_url"] = public_url(source["result_pr_url"])
    if "summary_status" in source:
        projected["summary_status"] = public_summary_string(source["summary_status"])
    return projected


def _project_preview_item(item: object) -> dict[str, object]:
    source = _require_dict(item)
    projected: dict[str, object] = {}
    status = source.get("status") or source.get("readiness_status")
    if status:
        projected["status"] = public_code(status)
    if "source_of_truth_url" in source:
        projected["source_of_truth_url"] = public_url(source["source_of_truth_url"])
    if "detail" in source:
        projected["detail"] = public_summary_string(source["detail"])
    return projected


def _project_list(value: object, projector) -> list[dict[str, object]]:  # type: ignore[no-untyped-def]
    if not isinstance(value, list):
        raise LaunchplaneSafetyError("invalid_response")
    return [projector(item) for item in value]


def _assert_context_source_safe(value: object) -> None:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            if is_denied_key(key) and key.strip().lower() != "payload":
                raise LaunchplaneSafetyError("unsafe_response_shape")
            _assert_context_source_safe(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_context_source_safe(item)
        return
    assert_public_safe_shape(value)


def normalize_section(section: object) -> dict[str, object]:
    if not isinstance(section, dict):
        return {"status": "unavailable", "reason_code": "invalid_response"}
    status = section.get("status")
    normalized: dict[str, object] = {
        "status": public_code(status, default="available")
    }
    reason_code = section.get("reason_code")
    if reason_code:
        normalized["reason_code"] = public_code(reason_code)
    return normalized


def _service_section_payload(section: dict[str, Any]) -> dict[str, Any]:
    if "payload" not in section:
        return section
    allowed = {"status", "payload", "reason_code"}
    if any(str(key) not in allowed for key in section):
        raise LaunchplaneSafetyError("unsafe_response_shape")
    payload = section.get("payload")
    if payload is None or payload == {}:
        return {}
    return _require_dict(payload)


def _nested_items(
    payload: dict[str, Any], container_key: str, items_key: str
) -> object:
    container = payload.get(container_key)
    if container is None:
        return payload.get(items_key)
    return _require_dict(container).get(items_key)


def project_context_sections(context: dict[str, Any]) -> dict[str, object]:
    sections: dict[str, object] = {}
    raw_sections = context.get("sections")
    source_sections = _require_dict(raw_sections) if raw_sections is not None else context
    source_map = (
        ("work_graph", "work_graph"),
        ("work_graph_snapshot", "work_graph"),
        ("repo_product_mapping", "repo_product_mapping"),
        ("every_code", "every_code"),
        ("every_code_summary", "every_code"),
        ("preview_readiness", "preview_readiness"),
    )
    for source_key, target_key in source_map:
        if source_key not in source_sections or target_key in sections:
            continue
        raw_section = _require_dict(source_sections[source_key])
        section = normalize_section(raw_section)
        source_section = _service_section_payload(raw_section)
        if target_key == "work_graph":
            items = _nested_items(source_section, "snapshot", "items")
            if items is None and "snapshot" in source_section:
                items = _require_dict(source_section["snapshot"]).get("issues")
            if items is not None:
                section["items"] = _project_list(items, _project_work_graph_item)
        elif target_key == "repo_product_mapping":
            repositories = _nested_items(source_section, "mapping", "repositories")
            if repositories is not None:
                section["repositories"] = _project_list(
                    repositories, _project_repo_mapping_item
                )
        elif target_key == "every_code":
            requests = _nested_items(source_section, "summary", "requests")
            if requests is None and "summary" in source_section:
                requests = _require_dict(source_section["summary"]).get("summaries")
            if requests is not None:
                section["requests"] = _project_list(
                    requests, _project_every_code_request
                )
        elif target_key == "preview_readiness":
            items = _nested_items(source_section, "readiness", "items")
            if items is not None:
                section["items"] = _project_list(items, _project_preview_item)
        sections[target_key] = section
    assert_public_safe_shape(sections)
    return sections


def normalize_launchplane_payload(
    provider_payload: dict[str, Any], *, request: dict[str, object]
) -> dict[str, object]:
    context = provider_payload.get("result", {}).get("context")
    if not isinstance(context, dict):
        context = provider_payload.get("context")
    if not isinstance(context, dict):
        raise ValueError("invalid_response")
    _assert_context_source_safe(context)

    payload = base_payload(status="available", request=request)
    generated_at = context.get("generated_at")
    if isinstance(generated_at, str) and generated_at:
        payload["generated_at"] = public_timestamp(generated_at)

    sections = project_context_sections(context)
    payload["sections"] = sections

    recommendation = "Use Launchplane context as a hint; verify GitHub source of truth."
    safe_to_start = None
    state = None
    source_of_truth_url = None
    work_graph = sections.get("work_graph")
    if isinstance(work_graph, dict):
        items = work_graph.get("items")
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, dict):
                recommendation = public_summary_string(first.get("next_action") or recommendation)
                safe_to_start = first.get("safe_to_start")
                state = first.get("state")
                source_of_truth_url = first.get("source_of_truth_url") or first.get("url")
    summary: dict[str, object] = {"recommendation": recommendation}
    if isinstance(safe_to_start, bool):
        summary["safe_to_start"] = safe_to_start
    if state:
        summary["state"] = public_code(state)
    if source_of_truth_url:
        summary["source_of_truth_url"] = public_url(source_of_truth_url)
        payload["links"] = [
            {"label": "Source of truth", "url": public_url(source_of_truth_url), "kind": "source"}
        ]
    trace_id = context.get("trace_id") or provider_payload.get("trace_id")
    if trace_id:
        summary["trace_id"] = public_trace_id(trace_id)
    assert_public_safe_shape(summary)
    payload["summary"] = summary
    return payload


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Emit optional Launchplane context JSON.")
    parser.add_argument("--repo", required=True, help="Repository in OWNER/REPO form.")
    parser.add_argument("--branch", help="Optional branch name.")
    parser.add_argument("--issue", type=int, help="Optional GitHub issue number.")
    parser.add_argument("--pr", type=int, help="Optional GitHub pull request number.")
    parser.add_argument("--config", help="Optional private JSON config path.")
    parser.add_argument("--url", help="Optional Launchplane service URL override.")
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout seconds.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    request = normalized_request(args)
    try:
        settings = resolve_settings(args)
    except ValueError:
        emit(unavailable_payload(request, status="invalid", code="invalid_config"))
        return 0
    try:
        validate_service_url(settings["service_url"])
    except LaunchplaneSafetyError as exc:
        if exc.code == "missing_service_url":
            if not settings["token"]:
                emit(no_context_payload(request))
                return 0
            emit(no_context_payload(request))
            return 0
        emit(unavailable_payload(request, status="invalid", code=exc.code))
        return 0
    if not settings["token"]:
        emit(no_context_payload(request))
        return 0
    try:
        provider_payload = request_launchplane(
            build_context_url(settings["service_url"], args), settings, args.timeout
        )
        emit(normalize_launchplane_payload(provider_payload, request=request))
        return 0
    except urllib.error.HTTPError as exc:
        status = "unauthorized" if exc.code in {401, 403} else "unavailable"
        code = "auth_required" if exc.code == 401 else "policy_denied" if exc.code == 403 else "provider_unavailable"
        emit(unavailable_payload(request, status=status, code=code))
        return 0
    except LaunchplaneSafetyError as exc:
        status = "unavailable" if exc.code == "unsafe_redirect" else "invalid"
        emit(unavailable_payload(request, status=status, code=exc.code))
        return 0
    except (OSError, TimeoutError, urllib.error.URLError):
        emit(unavailable_payload(request, status="unavailable", code="provider_unavailable"))
        return 0
    except (ValueError, json.JSONDecodeError):
        emit(unavailable_payload(request, status="invalid", code="invalid_response"))
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
