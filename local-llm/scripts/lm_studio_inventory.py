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
import sys
from pathlib import Path
from typing import Any

from lm_studio_api import (
    DEFAULT_BASE_URL,
    DEFAULT_CONFIG,
    LocalLLMError,
    fetch_lm_studio_runtime,
    fetch_openai_models,
    load_yaml,
    public_endpoint,
    resolve_endpoint,
)


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
        config = load_yaml(args.config)
        endpoint = resolve_endpoint(config, args.endpoint, args.base_url)
        models = fetch_openai_models(endpoint, args.timeout)
    except LocalLLMError as exc:
        payload = {"ok": False, "error": str(exc)}
        emit(payload, json_mode=args.json)
        return 1
    try:
        runtime = fetch_lm_studio_runtime(endpoint, args.timeout)
    except LocalLLMError as exc:
        runtime = {"available": False, "error": str(exc)}

    payload = {
        "ok": True,
        "endpoint": public_endpoint(endpoint),
        "models": models,
        "model_count": len(models),
        "runtime": runtime,
    }
    emit(payload, json_mode=args.json)
    return 0


def emit(payload: dict[str, Any], *, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if not payload.get("ok"):
        print(f"error: {payload.get('error')}", file=sys.stderr)
        return
    endpoint = payload["endpoint"]
    print(f"endpoint: {endpoint['id']} ({endpoint['locality']}, {endpoint['base_url']})")
    runtime = payload.get("runtime")
    if isinstance(runtime, dict):
        if runtime.get("available") is False or runtime.get("error"):
            print(f"loaded instances: unknown ({runtime.get('error', 'runtime unavailable')})")
        else:
            print(f"loaded instances: {runtime.get('loaded_instance_count', 0)}")
    for model in payload["models"]:
        print(model["id"])


if __name__ == "__main__":
    raise SystemExit(main())
