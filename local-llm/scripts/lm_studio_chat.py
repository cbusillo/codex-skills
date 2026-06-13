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
import re
import sys
from pathlib import Path
from typing import Any

from lm_studio_api import (
    DEFAULT_BASE_URL,
    DEFAULT_CONFIG,
    MODEL_INDEX,
    LocalLLMError,
    load_lm_studio_model,
    load_yaml,
    parse_float_option,
    parse_int_option,
    post_json,
    public_endpoint,
    resolve_endpoint,
    resolve_role,
    role_model,
    unload_lm_studio_model,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MAX_INPUT_CHARS = 12_000
CHANNEL_RE = re.compile(r"<\|channel\|>\w+\s*(?:<\|constrain\|>\w+)?\s*<\|message\|>", re.I)


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
    parser.add_argument("--load-policy", choices=("none", "jit_chat", "api_explicit"), help="Model lifecycle policy. Defaults to role load_policy or none.")
    parser.add_argument("--ttl", type=int, help="TTL seconds for LM Studio JIT chat loading.")
    parser.add_argument("--context-length", type=int, help="Context length for LM Studio api_explicit load policy.")
    parser.add_argument("--flash-attention", action="store_true", help="Request flash attention for LM Studio api_explicit load.")
    parser.add_argument("--warmup", action="store_true", help="Run a harmless probe before sending the prompt.")
    parser.add_argument("--unload-after", action="store_true", help="Unload the instance loaded by api_explicit after the chat request.")
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
    lifecycle = prepare_lifecycle(endpoint, model, args, role, timeout)
    response: dict[str, Any] | None = None
    try:
        if args.warmup:
            warmup_payload = chat_payload(model, "Reply with exactly: OK", "You are a readiness probe.", temperature, 16)
            if lifecycle.get("load_policy") == "jit_chat" and lifecycle.get("ttl_seconds"):
                warmup_payload["ttl"] = lifecycle["ttl_seconds"]
            warmup_response = post_json(
                f"{endpoint['base_url']}/chat/completions",
                warmup_payload,
                endpoint,
                timeout,
                error_context="warmup chat request failed",
            )
            lifecycle["warmup_served_model"] = warmup_response.get("model")
        payload = chat_payload(model, prompt, system, temperature, max_tokens)
        if lifecycle.get("load_policy") == "jit_chat" and lifecycle.get("ttl_seconds"):
            payload["ttl"] = lifecycle["ttl_seconds"]
        response = post_json(
            f"{endpoint['base_url']}/chat/completions",
            payload,
            endpoint,
            timeout,
            error_context=f"chat request failed for {endpoint.get('id')}",
        )
    finally:
        if args.unload_after and lifecycle.get("loaded_instance_id"):
            lifecycle["unload_response"] = unload_lm_studio_model(endpoint, str(lifecycle["loaded_instance_id"]), timeout)
    if response is None:
        raise LocalLLMError("chat request failed before a response was returned")
    content = extract_content(response)
    if not content:
        raise LocalLLMError("endpoint returned no assistant content; increase --max-tokens for reasoning models")
    return {
        "ok": True,
        "model": model,
        "served_model": response.get("model"),
        "role": role.get("name"),
        "endpoint": public_endpoint(endpoint),
        "prompt_chars": len(prompt),
        "max_tokens": max_tokens,
        "lifecycle": lifecycle,
        "content": content,
    }


def chat_payload(model: str, prompt: str, system: str, temperature: float, max_tokens: int) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }


def prepare_lifecycle(endpoint: dict[str, Any], model: str, args: argparse.Namespace, role: dict[str, Any], timeout: float) -> dict[str, Any]:
    raw_load_config = role.get("load")
    load_config: dict[str, Any] = raw_load_config if isinstance(raw_load_config, dict) else {}
    policy = normalize_load_policy(args.load_policy or str(role.get("load_policy") or load_config.get("policy") or "none"))
    ttl_source = role.get("ttl_seconds") or load_config.get("ttl_seconds")
    ttl = parse_int_option(args.ttl, ttl_source, 0, "ttl") if args.ttl or ttl_source else None
    lifecycle: dict[str, Any] = {"load_policy": policy, "ttl_seconds": ttl}
    if policy == "none":
        return lifecycle
    if policy == "jit_chat":
        if endpoint.get("provider") != "lm_studio":
            raise LocalLLMError("jit_chat load policy requires provider=lm_studio")
        return lifecycle
    if policy == "api_explicit":
        context_source = role.get("context_length") or load_config.get("context_length")
        context_length = (
            parse_int_option(args.context_length, context_source, 0, "context_length")
            if args.context_length or context_source
            else None
        )
        flash_attention = True if args.flash_attention else load_config.get("flash_attention")
        if ttl:
            lifecycle["ttl_note"] = "ttl is not sent to LM Studio native load; use --unload-after for explicit cleanup"
        response = load_lm_studio_model(
            endpoint,
            model,
            timeout,
            context_length=context_length,
            flash_attention=flash_attention,
            echo_load_config=True,
        )
        lifecycle.update(
            {
                "loaded_instance_id": response.get("instance_id"),
                "load_response": {
                    "status": response.get("status"),
                    "load_time_seconds": response.get("load_time_seconds"),
                    "load_config": response.get("load_config"),
                },
            }
        )
        return lifecycle
    raise LocalLLMError(f"unknown load policy: {policy}")


def normalize_load_policy(policy: str) -> str:
    aliases = {"jit": "jit_chat", "explicit": "api_explicit", "native": "api_explicit"}
    return aliases.get(policy, policy)


def extract_content(response: dict[str, Any]) -> str:
    choices = response.get("choices") if isinstance(response, dict) else None
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else ""
    return CHANNEL_RE.sub("", str(content or "")).strip()


def emit(payload: dict[str, Any], *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif payload.get("ok"):
        print(payload["content"])
    else:
        print(f"error: {payload.get('error')}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
