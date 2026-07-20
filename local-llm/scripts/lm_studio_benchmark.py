#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Benchmark local LM Studio/OpenAI-compatible model response behavior."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from lm_studio_chat import LocalLLMError, chat, load_yaml, resolve_endpoint, resolve_role
from lm_studio_inventory import DEFAULT_CONFIG


SCRIPT_DIR = Path(__file__).resolve().parent
MODEL_INDEX = SCRIPT_DIR.parent / "references" / "model-index.yaml"
DEFAULT_PROMPT = "Reply with exactly: OK"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark one or more local chat models.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--endpoint")
    parser.add_argument("--base-url")
    parser.add_argument("--model", action="append", dest="models", help="Model id to test. May be repeated.")
    parser.add_argument("--role", help="Role whose model candidates should be tested.")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--load-policy", choices=("none", "jit_chat", "api_explicit"))
    parser.add_argument("--ttl", type=int)
    parser.add_argument("--context-length", type=int)
    parser.add_argument("--flash-attention", action="store_true")
    parser.add_argument("--warmup", action="store_true")
    parser.add_argument("--unload-after", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = load_yaml(args.config)
        index = load_yaml(MODEL_INDEX)
        role = resolve_role(config, index, args.role) if args.role else {}
        endpoint = resolve_endpoint(config, args.endpoint, args.base_url, role)
        models = args.models or role_models(role)
        if not models:
            raise LocalLLMError("pass --model or --role with configured model candidates")
        results = [run_one(endpoint, role, model, args) for model in models]
    except LocalLLMError as exc:
        payload = {"ok": False, "error": str(exc)}
        emit(payload, json_mode=args.json)
        return 1
    payload = {"ok": True, "endpoint": endpoint.get("id"), "role": role.get("name"), "results": results}
    emit(payload, json_mode=args.json)
    return 0


def role_models(role: dict[str, Any]) -> list[str]:
    values = []
    for key in ("primary", "fast", "deep", "small", "fallback", "model"):
        value = role.get(key)
        if isinstance(value, str) and value.strip() and value not in values:
            values.append(value.strip())
    return values


def run_one(endpoint: dict[str, Any], role: dict[str, Any], model: str, args: argparse.Namespace) -> dict[str, Any]:
    bench_args = argparse.Namespace(
        max_tokens=args.max_tokens,
        timeout=args.timeout,
        temperature=0,
        load_policy=args.load_policy,
        ttl=args.ttl,
        context_length=args.context_length,
        flash_attention=args.flash_attention,
        warmup=args.warmup,
        unload_after=args.unload_after,
    )
    started = time.monotonic()
    try:
        result = chat(endpoint, model, args.prompt, "You are a benchmark responder.", bench_args, role)
        elapsed = time.monotonic() - started
        return {
            "model": model,
            "ok": True,
            "elapsed_seconds": round(elapsed, 3),
            "content_chars": len(result["content"]),
            "content_preview": result["content"][:80],
            "served_model": result.get("served_model"),
            "lifecycle": result.get("lifecycle"),
        }
    except LocalLLMError as exc:
        elapsed = time.monotonic() - started
        return {"model": model, "ok": False, "elapsed_seconds": round(elapsed, 3), "error": str(exc)}


def emit(payload: dict[str, Any], *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if not payload.get("ok"):
        print(f"error: {payload.get('error')}", file=sys.stderr)
        return
    for result in payload["results"]:
        status = "ok" if result["ok"] else "failed"
        detail = result.get("content_preview") or result.get("error", "")
        print(f"{result['model']}: {status} in {result['elapsed_seconds']}s {detail}")


if __name__ == "__main__":
    raise SystemExit(main())
