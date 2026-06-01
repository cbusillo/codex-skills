#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML>=6.0.0",
# ]
# ///
"""Send one bounded chat prompt to a local OpenAI-compatible endpoint."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[1]
DEFAULT_CONFIG = ROOT / ".local" / "local-llm.yaml"
MODEL_INDEX = SCRIPT_DIR.parent / "references" / "model-index.yaml"
DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_MAX_INPUT_CHARS = 12_000
CHANNEL_RE = re.compile(r"<\|channel\|>\w+\s*(?:<\|constrain\|>\w+)?\s*<\|message\|>", re.I)


class LocalLLMError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one local LLM chat completion.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--endpoint")
    parser.add_argument("--base-url")
    parser.add_argument("--model", help="Explicit model id. Overrides --role.")
    parser.add_argument("--role", help="Role from model index/private config.")
    parser.add_argument("--prompt", help="Prompt text. Defaults to stdin when omitted.")
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--system", default="You are a concise local assistant. Use only the provided prompt.")
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--max-input-chars", type=int, default=DEFAULT_MAX_INPUT_CHARS)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.prompt and args.prompt_file:
        parser.error("use --prompt or --prompt-file, not both")
    if args.max_input_chars <= 0:
        parser.error("--max-input-chars must be positive")
    return args


def main() -> int:
    args = parse_args()
    try:
        config = load_yaml(args.config)
        index = load_yaml(MODEL_INDEX)
        role = resolve_role(config, index, args.role)
        endpoint = resolve_endpoint(config, args.endpoint, args.base_url, role)
        model = args.model or role_model(role)
        if not model:
            raise LocalLLMError("pass --model or configure a role with a primary model")
        prompt = read_prompt(args)[: args.max_input_chars]
        result = chat(endpoint, model, prompt, args.system, args, role)
    except LocalLLMError as exc:
        payload = {"ok": False, "error": str(exc)}
        emit(payload, json_mode=args.json)
        return 1
    emit(result, json_mode=args.json)
    return 0


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
    public_roles = index.get("model_roles") if isinstance(index.get("model_roles"), dict) else {}
    private_roles = config.get("model_roles") if isinstance(config.get("model_roles"), dict) else {}
    merged: dict[str, Any] = {}
    if isinstance(public_roles.get(role_name), dict):
        merged.update(public_roles[role_name])
    if isinstance(private_roles.get(role_name), dict):
        merged.update(private_roles[role_name])
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


def resolve_endpoint(config: dict[str, Any], endpoint_id: str | None, base_url: str | None, role: dict[str, Any]) -> dict[str, Any]:
    if base_url:
        return {
            "id": "cli",
            "provider": "openai_compatible",
            "base_url": base_url.rstrip("/"),
            "locality": infer_locality(base_url),
            "trust": "cli_override",
        }
    endpoints = config.get("endpoints") if isinstance(config.get("endpoints"), dict) else {}
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
        }
    if endpoint.get("enabled") is False:
        raise LocalLLMError(f"endpoint {endpoint.get('id')} is disabled")
    endpoint["base_url"] = str(endpoint.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    locality = str(endpoint.get("locality") or infer_locality(endpoint["base_url"])).strip()
    endpoint["locality"] = locality if locality in {"localhost", "trusted_lan", "remote_private", "cloud"} else "remote_private"
    return endpoint


def infer_locality(base_url: str) -> str:
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").casefold()
    if host in {"127.0.0.1", "::1", "localhost"}:
        return "localhost"
    return "cloud"


def read_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file:
        try:
            return args.prompt_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise LocalLLMError(f"unable to read prompt file: {exc}") from exc
    if args.prompt is not None:
        return args.prompt
    return sys.stdin.read()


def chat(endpoint: dict[str, Any], model: str, prompt: str, system: str, args: argparse.Namespace, role: dict[str, Any]) -> dict[str, Any]:
    max_tokens = parse_int_option(args.max_tokens, role.get("max_tokens"), 900, "max_tokens")
    timeout = parse_float_option(args.timeout, role.get("timeout_seconds"), 120, "timeout_seconds")
    temperature = parse_float_option(args.temperature, role.get("temperature"), 0.2, "temperature")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    response = post_json(f"{endpoint['base_url']}/chat/completions", payload, endpoint, timeout)
    error = response.get("error") if isinstance(response, dict) else None
    if error:
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise LocalLLMError(f"API error: {message}")
    content = extract_content(response)
    if not content:
        raise LocalLLMError("endpoint returned no assistant content; increase --max-tokens for reasoning models")
    return {
        "ok": True,
        "model": model,
        "role": role.get("name"),
        "endpoint": public_endpoint(endpoint),
        "prompt_chars": len(prompt),
        "max_tokens": max_tokens,
        "content": content,
    }


def post_json(url: str, payload: dict[str, Any], endpoint: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **request_headers(endpoint)})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise LocalLLMError(f"chat request failed for {endpoint.get('id')}: {exc}") from exc


def request_headers(endpoint: dict[str, Any]) -> dict[str, str]:
    token_env = endpoint.get("token_env")
    if isinstance(token_env, str) and token_env.strip() and os.environ.get(token_env.strip()):
        return {"Authorization": f"Bearer {os.environ[token_env.strip()]}"}
    return {}


def extract_content(response: dict[str, Any]) -> str:
    choices = response.get("choices") if isinstance(response, dict) else None
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else ""
    return CHANNEL_RE.sub("", str(content or "")).strip()


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
    elif payload.get("ok"):
        print(payload["content"])
    else:
        print(f"error: {payload.get('error')}", file=sys.stderr)


def parse_int_option(cli_value: int | None, role_value: object, default: int, name: str) -> int:
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


def parse_float_option(cli_value: float | None, role_value: object, default: float, name: str) -> float:
    value = cli_value if cli_value is not None else role_value
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise LocalLLMError(f"{name} must be numeric") from exc


def public_base_url(endpoint: dict[str, Any]) -> str:
    base_url = str(endpoint.get("base_url") or "")
    if endpoint.get("locality") == "localhost":
        return base_url
    return f"[redacted:{endpoint.get('locality') or 'remote'}]"


if __name__ == "__main__":
    raise SystemExit(main())
