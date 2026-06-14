"""Shared LM Studio/OpenAI-compatible endpoint helpers."""

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML>=6.0.0",
# ]
# ///

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import yaml  # type: ignore[import-untyped]


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / ".local" / "local-llm.yaml"
MODEL_INDEX = Path(__file__).resolve().parent.parent / "references" / "model-index.yaml"
DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
LOCALITIES = {"localhost", "trusted_lan", "remote_private", "cloud"}


class LocalLLMError(RuntimeError):
    pass


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise LocalLLMError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise LocalLLMError(f"{path} must contain a YAML mapping")
    return data


def resolve_role(config: dict[str, Any], index: dict[str, Any], role_name: str | None) -> dict[str, Any]:
    if not role_name:
        return {}
    public_roles_raw = index.get("model_roles")
    private_roles_raw = config.get("model_roles")
    public_roles = public_roles_raw if isinstance(public_roles_raw, dict) else {}
    private_roles = private_roles_raw if isinstance(private_roles_raw, dict) else {}
    merged: dict[str, Any] = {}
    public_role = public_roles.get(role_name)
    private_role = private_roles.get(role_name)
    if isinstance(public_role, dict):
        merged.update(public_role)
    if isinstance(private_role, dict):
        merged.update(private_role)
    if not merged:
        raise LocalLLMError(f"unknown model role: {role_name}")
    merged["name"] = role_name
    return merged


def role_model(role: dict[str, Any]) -> str | None:
    for key in ("primary", "model", "fast", "fallback"):
        value = role.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_endpoint(config: dict[str, Any], endpoint_id: str | None, base_url: str | None, role: dict[str, Any] | None = None) -> dict[str, Any]:
    role = role or {}
    if base_url:
        return normalize_endpoint(
            {
                "id": "cli",
                "provider": infer_provider(base_url),
                "base_url": base_url,
                "locality": infer_locality(base_url),
                "trust": "cli_override",
            }
        )
    endpoints_raw = config.get("endpoints")
    endpoints = endpoints_raw if isinstance(endpoints_raw, dict) else {}
    selected = endpoint_id or role.get("endpoint") or config.get("default_endpoint")
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
    return normalize_endpoint(endpoint)


def normalize_endpoint(endpoint: dict[str, Any]) -> dict[str, Any]:
    endpoint = dict(endpoint)
    endpoint["base_url"] = normalize_base_url(str(endpoint.get("base_url") or DEFAULT_BASE_URL))
    locality = str(endpoint.get("locality") or infer_locality(endpoint["base_url"])).strip()
    endpoint["locality"] = locality if locality in LOCALITIES else "remote_private"
    if endpoint.get("provider") == "lm_studio":
        endpoint["native_base_url"] = normalize_base_url(
            str(endpoint.get("native_base_url") or derive_lm_studio_native_base_url(endpoint["base_url"]))
        )
    return endpoint


def infer_locality(base_url: str) -> str:
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").casefold()
    if host in {"127.0.0.1", "::1", "localhost"}:
        return "localhost"
    return "cloud"


def infer_provider(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.port == 1234:
        return "lm_studio"
    return "openai_compatible"


def normalize_base_url(value: str) -> str:
    return value.rstrip("/")


def derive_lm_studio_native_base_url(base_url: str) -> str:
    parsed = urlparse(base_url.rstrip("/"))
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    elif path == "/v1":
        path = ""
    native_path = f"{path}/api/v1" if path else "/api/v1"
    return urlunparse(parsed._replace(path=native_path, params="", query="", fragment="")).rstrip("/")


def request_headers(endpoint: dict[str, Any], *, accept_json: bool = False) -> dict[str, str]:
    headers: dict[str, str] = {}
    if accept_json:
        headers["Accept"] = "application/json"
    token_env = endpoint.get("token_env")
    if isinstance(token_env, str) and token_env.strip() and os.environ.get(token_env.strip()):
        headers["Authorization"] = f"Bearer {os.environ[token_env.strip()]}"
    return headers


def get_json(url: str, endpoint: dict[str, Any], timeout: float, *, error_context: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=request_headers(endpoint, accept_json=True))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = read_http_error(exc)
        raise LocalLLMError(f"{error_context}: HTTP {exc.code}{detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise LocalLLMError(f"{error_context}: {exc}") from exc
    if not isinstance(payload, dict):
        raise LocalLLMError(f"{error_context}: response JSON was not an object")
    return payload


def post_json(url: str, payload: dict[str, Any], endpoint: dict[str, Any], timeout: float, *, error_context: str) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **request_headers(endpoint)},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            parsed = json.loads(response.read().decode("utf-8", errors="replace"))
    except TimeoutError as exc:
        raise LocalLLMError(f"{error_context}: request timed out after {timeout:g}s") from exc
    except urllib.error.HTTPError as exc:
        detail = read_http_error(exc)
        raise LocalLLMError(f"{error_context}: HTTP {exc.code}{detail}") from exc
    except urllib.error.URLError as exc:
        raise LocalLLMError(f"{error_context}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise LocalLLMError(f"{error_context}: response was not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LocalLLMError(f"{error_context}: response JSON was not an object")
    error = parsed.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise LocalLLMError(f"{error_context}: API error: {message}")
    return parsed


def read_http_error(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""
    if not body.strip():
        return ""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return f": {body[:500]}"
    return f": {json.dumps(payload, ensure_ascii=False, sort_keys=True)[:500]}"


def fetch_openai_models(endpoint: dict[str, Any], timeout: float) -> list[dict[str, Any]]:
    payload = get_json(f"{endpoint['base_url']}/models", endpoint, timeout, error_context=f"unable to query models from {endpoint.get('id')}")
    data = payload.get("data")
    if not isinstance(data, list):
        raise LocalLLMError("/models response did not contain a data list")
    models = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            models.append({"id": item["id"], "object": item.get("object")})
    return sorted(models, key=lambda model: model["id"])


def fetch_lm_studio_runtime(endpoint: dict[str, Any], timeout: float) -> dict[str, Any] | None:
    if endpoint.get("provider") != "lm_studio" or not endpoint.get("native_base_url"):
        return None
    payload = get_json(f"{endpoint['native_base_url']}/models", endpoint, timeout, error_context="unable to query LM Studio runtime models")
    models = payload.get("models")
    if not isinstance(models, list):
        models = payload.get("data")
    if not isinstance(models, list):
        raise LocalLLMError("LM Studio runtime response did not contain a models list")
    loaded: list[dict[str, Any]] = []
    loaded_instance_count = 0
    for model in models:
        if not isinstance(model, dict):
            continue
        instances = model.get("loaded_instances") or model.get("loadedInstances") or []
        if isinstance(instances, list) and instances:
            loaded.append({"key": model.get("key"), "loaded_instances": instances})
            loaded_instance_count += len(instances)
        elif model.get("state") == "loaded" or model.get("loaded") is True:
            loaded.append({"key": model.get("key") or model.get("id"), "model": model})
            loaded_instance_count += 1
    return {"models": models, "loaded_instances": loaded, "loaded_instance_count": loaded_instance_count}


def load_lm_studio_model(endpoint: dict[str, Any], model: str, timeout: float, **options: Any) -> dict[str, Any]:
    require_lm_studio_native(endpoint)
    payload: dict[str, Any] = {"model": model}
    for key in ("context_length", "flash_attention", "echo_load_config"):
        value = options.get(key)
        if value is not None:
            payload[key] = value
    return post_json(f"{endpoint['native_base_url']}/models/load", payload, endpoint, timeout, error_context="LM Studio model load failed")


def unload_lm_studio_model(endpoint: dict[str, Any], instance_id: str, timeout: float) -> dict[str, Any]:
    require_lm_studio_native(endpoint)
    if not instance_id.strip():
        raise LocalLLMError("instance_id is required for LM Studio unload")
    return post_json(
        f"{endpoint['native_base_url']}/models/unload",
        {"instance_id": instance_id.strip()},
        endpoint,
        timeout,
        error_context="LM Studio model unload failed",
    )


def require_lm_studio_native(endpoint: dict[str, Any]) -> None:
    if endpoint.get("provider") != "lm_studio" or not endpoint.get("native_base_url"):
        raise LocalLLMError("LM Studio native lifecycle requires provider=lm_studio and native_base_url")


def public_endpoint(endpoint: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": endpoint.get("id"),
        "provider": endpoint.get("provider"),
        "base_url": public_base_url(endpoint),
        "native_base_url": public_native_base_url(endpoint),
        "locality": endpoint.get("locality"),
        "trust": endpoint.get("trust"),
        "uses_token_env": bool(endpoint.get("token_env")),
    }


def public_base_url(endpoint: dict[str, Any]) -> str:
    base_url = str(endpoint.get("base_url") or "")
    if endpoint.get("locality") == "localhost":
        return base_url
    return f"[redacted:{endpoint.get('locality') or 'remote'}]"


def public_native_base_url(endpoint: dict[str, Any]) -> str | None:
    native = endpoint.get("native_base_url")
    if not native:
        return None
    if endpoint.get("locality") == "localhost":
        return str(native)
    return f"[redacted:{endpoint.get('locality') or 'remote'}]"


def parse_int_option(cli_value: int | None, role_value: Any, default: int, name: str) -> int:
    value = cli_value if cli_value is not None else role_value
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise LocalLLMError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise LocalLLMError(f"{name} must be positive")
    return parsed


def parse_float_option(cli_value: float | None, role_value: Any, default: float, name: str) -> float:
    value = cli_value if cli_value is not None else role_value
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise LocalLLMError(f"{name} must be numeric") from exc
