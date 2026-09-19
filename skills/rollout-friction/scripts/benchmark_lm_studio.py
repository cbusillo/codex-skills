#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Benchmark local LM Studio chat model latency for rollout scouts."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
FALLBACK_MODELS = [
    "openai/gpt-oss-20b",
    "google/gemma-4-31b",
    "qwen/qwen3.6-27b",
    "qwen/qwen3.6-35b-a3b",
    "openai/gpt-oss-120b",
    "qwen_qwen3.5-122b-a10b",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark LM Studio model response latency.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", action="append", dest="models")
    parser.add_argument(
        "--all-models",
        action="store_true",
        help="Benchmark every non-embedding model currently reported by LM Studio.",
    )
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--fast-timeout", type=float, default=45)
    parser.add_argument("--warm-runs", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--json", action="store_true", help="Emit JSON lines.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    models = choose_models(args)
    for model in models:
        cold = run_once(args.base_url, model, args.timeout, args.max_tokens, "cold")
        emit(cold, args.json)
        fast = run_once(args.base_url, model, args.fast_timeout, args.max_tokens, "fast-budget")
        emit(fast, args.json)
        for index in range(args.warm_runs):
            warm = run_once(args.base_url, model, args.timeout, args.max_tokens, f"warm-{index + 1}")
            emit(warm, args.json)
    return 0


def choose_models(args: argparse.Namespace) -> list[str]:
    if args.models:
        return args.models
    env_models = os.environ.get("ROLLOUT_FRICTION_LM_BENCH_MODELS")
    if env_models:
        return [model.strip() for model in env_models.split(",") if model.strip()]
    discovered = list_models(args.base_url)
    if args.all_models:
        return discovered or FALLBACK_MODELS
    if discovered:
        preferred = [model for model in discovered if model == "openai/gpt-oss-20b"]
        if preferred:
            return preferred
        return discovered[:1]
    return FALLBACK_MODELS[:1]


def list_models(base_url: str) -> list[str]:
    try:
        parsed = get_json(f"{base_url.rstrip('/')}/models", timeout=10)
    except Exception:  # noqa: BLE001 - benchmark falls back when discovery is unavailable.
        return []
    data = parsed.get("data")
    if not isinstance(data, list):
        return []
    models: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model = item.get("id")
        if isinstance(model, str) and "embedding" not in model.lower():
            models.append(model)
    return models


def run_once(base_url: str, model: str, timeout: float, max_tokens: int, phase: str) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": False,
    }
    started = time.monotonic()
    try:
        parsed = post_json(f"{base_url.rstrip('/')}/chat/completions", payload, timeout)
    except Exception as exc:  # noqa: BLE001 - benchmark reports tool errors directly.
        elapsed = time.monotonic() - started
        return {
            "model": model,
            "phase": phase,
            "ok": False,
            "elapsed_seconds": round(elapsed, 3),
            "error": str(exc),
        }
    elapsed = time.monotonic() - started
    choice = (parsed.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = parsed.get("usage") or {}
    return {
        "model": model,
        "phase": phase,
        "ok": True,
        "elapsed_seconds": round(elapsed, 3),
        "finish_reason": choice.get("finish_reason"),
        "content_chars": len(message.get("content") or ""),
        "reasoning_chars": len(message.get("reasoning") or ""),
        "total_tokens": usage.get("total_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
    }


def post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except TimeoutError as exc:
        raise RuntimeError(f"timeout after {timeout:g}s") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"request failed: {exc.reason}") from exc
    parsed = json.loads(body)
    if isinstance(parsed, dict) and parsed.get("error"):
        raise RuntimeError(f"model error: {parsed['error']}")
    if not isinstance(parsed, dict):
        raise RuntimeError("response JSON was not an object")
    return parsed


def get_json(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise RuntimeError("response JSON was not an object")
    return parsed


def emit(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, sort_keys=True))
        return
    status = "ok" if result["ok"] else "error"
    detail = result.get("error") or (
        f"finish={result.get('finish_reason')} total_tokens={result.get('total_tokens')} "
        f"completion={result.get('completion_tokens')} reasoning={result.get('reasoning_tokens')}"
    )
    print(f"{result['model']} {result['phase']} {status} {result['elapsed_seconds']}s {detail}")


if __name__ == "__main__":
    raise SystemExit(main())
