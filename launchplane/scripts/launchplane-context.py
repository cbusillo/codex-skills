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
    base = service_url.rstrip("/")
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
    return f"{base}/v1/agent/context" + (f"?{encoded}" if encoded else "")


def request_launchplane(url: str, settings: dict[str, str], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/json")
    request.add_header("Authorization", f"Bearer {settings['token']}")
    if settings.get("subject"):
        request.add_header("X-Launchplane-Agent-Subject", settings["subject"])
    if settings.get("token_label"):
        request.add_header("X-Launchplane-Agent-Token-Label", settings["token_label"])
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid_response")
    return payload


def normalize_section(section: object) -> dict[str, object]:
    if not isinstance(section, dict):
        return {"status": "unavailable", "reason_code": "invalid_response"}
    status = section.get("status")
    normalized: dict[str, object] = {
        "status": status if isinstance(status, str) and status else "available"
    }
    reason_code = section.get("reason_code")
    if isinstance(reason_code, str) and reason_code:
        normalized["reason_code"] = reason_code
    data = section.get("data")
    if isinstance(data, dict):
        normalized.update(data)
    for key in ("items", "repositories", "requests", "readiness", "summary"):
        value = section.get(key)
        if isinstance(value, (list, dict, str, int, float, bool)) or value is None:
            if value is not None:
                normalized[key] = value
    return normalized


def normalize_launchplane_payload(
    provider_payload: dict[str, Any], *, request: dict[str, object]
) -> dict[str, object]:
    context = provider_payload.get("result", {}).get("context")
    if not isinstance(context, dict):
        context = provider_payload.get("context")
    if not isinstance(context, dict):
        raise ValueError("invalid_response")

    payload = base_payload(status="available", request=request)
    generated_at = context.get("generated_at")
    if isinstance(generated_at, str) and generated_at:
        payload["generated_at"] = generated_at

    sections: dict[str, object] = {}
    section_map = {
        "work_graph": "work_graph",
        "work_graph_snapshot": "work_graph",
        "repo_product_mapping": "repo_product_mapping",
        "every_code": "every_code",
        "every_code_summary": "every_code",
        "preview_readiness": "preview_readiness",
    }
    for source_key, target_key in section_map.items():
        if source_key in context and target_key not in sections:
            sections[target_key] = normalize_section(context[source_key])
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
                recommendation = str(first.get("next_action") or recommendation)
                safe_to_start = first.get("safe_to_start")
                state = first.get("state")
                source_of_truth_url = first.get("source_of_truth_url") or first.get("url")
    summary: dict[str, object] = {"recommendation": recommendation}
    if isinstance(safe_to_start, bool):
        summary["safe_to_start"] = safe_to_start
    if isinstance(state, str) and state:
        summary["state"] = state
    if isinstance(source_of_truth_url, str) and source_of_truth_url:
        summary["source_of_truth_url"] = source_of_truth_url
        payload["links"] = [
            {"label": "Source of truth", "url": source_of_truth_url, "kind": "source"}
        ]
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
    if not settings["service_url"] or not settings["token"]:
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
    except (OSError, TimeoutError, urllib.error.URLError):
        emit(unavailable_payload(request, status="unavailable", code="provider_unavailable"))
        return 0
    except (ValueError, json.JSONDecodeError):
        emit(unavailable_payload(request, status="invalid", code="invalid_response"))
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))