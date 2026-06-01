#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML>=6.0.0",
# ]
# ///
"""List models from a local OpenAI-compatible LM Studio endpoint."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / ".local" / "local-llm.yaml"
DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
LOCALITIES = {"localhost", "trusted_lan", "remote_private", "cloud"}


class LocalLLMError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List models from an LM Studio/OpenAI-compatible endpoint.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--endpoint", help="Endpoint id from private config.")
    parser.add_argument("--base-url", help="Override endpoint base URL.")
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args.config)
        endpoint = resolve_endpoint(config, args.endpoint, args.base_url)
        models = fetch_models(endpoint, args.timeout)
    except LocalLLMError as exc:
        payload = {"ok": False, "error": str(exc)}
        emit(payload, json_mode=args.json)
        return 1

    payload = {
        "ok": True,
        "endpoint": public_endpoint(endpoint),
        "models": models,
        "model_count": len(models),
    }
    emit(payload, json_mode=args.json)
    return 0


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise LocalLLMError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LocalLLMError("local LLM config must be a YAML mapping")
    return data


def resolve_endpoint(config: dict[str, Any], endpoint_id: str | None, base_url: str | None) -> dict[str, Any]:
    if base_url:
        return {
            "id": "cli",
            "provider": "openai_compatible",
            "base_url": normalize_base_url(base_url),
            "locality": infer_locality(base_url),
            "trust": "cli_override",
        }
    endpoints = config.get("endpoints") if isinstance(config.get("endpoints"), dict) else {}
    selected = endpoint_id or config.get("default_endpoint")
    if endpoint_id and endpoint_id not in endpoints:
        raise LocalLLMError(f"endpoint not found in local config: {endpoint_id}")
    if selected and selected not in endpoints:
        raise LocalLLMError(f"default endpoint not found in local config: {selected}")
    if selected and selected in endpoints:
        endpoint = dict(endpoints[selected] or {})
        endpoint["id"] = selected
    else:
        endpoint = {
            "id": "default-localhost",
            "provider": "lm_studio",
            "base_url": DEFAULT_BASE_URL,
            "locality": "localhost",
            "trust": "private_local",
            "enabled": True,
        }
    if endpoint.get("enabled") is False:
        raise LocalLLMError(f"endpoint {endpoint.get('id')} is disabled")
    endpoint["base_url"] = normalize_base_url(str(endpoint.get("base_url") or DEFAULT_BASE_URL))
    locality = str(endpoint.get("locality") or infer_locality(endpoint["base_url"])).strip()
    endpoint["locality"] = locality if locality in LOCALITIES else "remote_private"
    return endpoint


def infer_locality(base_url: str) -> str:
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").casefold()
    if host in {"127.0.0.1", "::1", "localhost"}:
        return "localhost"
    return "cloud"


def normalize_base_url(value: str) -> str:
    return value.rstrip("/")


def fetch_models(endpoint: dict[str, Any], timeout: float) -> list[dict[str, Any]]:
    url = f"{endpoint['base_url']}/models"
    request = urllib.request.Request(url, headers=request_headers(endpoint))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise LocalLLMError(f"unable to query models from {endpoint['id']}: {exc}") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise LocalLLMError("/models response did not contain a data list")
    models = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            models.append({"id": item["id"], "object": item.get("object")})
    return sorted(models, key=lambda model: model["id"])


def request_headers(endpoint: dict[str, Any]) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    token_env = endpoint.get("token_env")
    if isinstance(token_env, str) and token_env.strip():
        token = os.environ.get(token_env.strip())
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def public_endpoint(endpoint: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": endpoint.get("id"),
        "provider": endpoint.get("provider"),
        "base_url": public_base_url(endpoint),
        "locality": endpoint.get("locality"),
        "trust": endpoint.get("trust"),
        "uses_token_env": bool(endpoint.get("token_env")),
    }


def emit(payload: dict[str, Any], *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if not payload.get("ok"):
        print(f"error: {payload.get('error')}", file=sys.stderr)
        return
    endpoint = payload["endpoint"]
    print(f"endpoint: {endpoint['id']} ({endpoint['locality']}, {endpoint['base_url']})")
    for model in payload["models"]:
        print(model["id"])


def public_base_url(endpoint: dict[str, Any]) -> str:
    base_url = str(endpoint.get("base_url") or "")
    if endpoint.get("locality") == "localhost":
        return base_url
    return f"[redacted:{endpoint.get('locality') or 'remote'}]"


if __name__ == "__main__":
    raise SystemExit(main())
