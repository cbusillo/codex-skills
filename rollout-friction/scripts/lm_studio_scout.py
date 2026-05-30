#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Ask a local LM Studio model for rollout-friction scout hypotheses.

This helper intentionally accepts redacted summaries, not raw rollout traces.
It is an advisory second pass: callers must verify suggestions before making
skill, repo, harness, or local-config changes.
"""

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


DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_MODEL = os.environ.get("ROLLOUT_FRICTION_LM_MODEL", "openai/gpt-oss-20b")
DEFAULT_TIMEOUT = 45
DEEP_TIMEOUT = 180
DEFAULT_MAX_INPUT_CHARS = 12_000
DEFAULT_MAX_TOKENS = 900

CHANNEL_RE = re.compile(r"<\|channel\|>\w+\s*(?:<\|constrain\|>\w+)?\s*<\|message\|>", re.I)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a bounded local LM Studio scout over a redacted rollout-friction report."
    )
    parser.add_argument("report", type=Path, help="Redacted analyzer report or synthesized notes.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Use a longer timeout for deliberate large-model/cold-load scout runs.",
    )
    parser.add_argument("--max-input-chars", type=int, default=DEFAULT_MAX_INPUT_CHARS)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--json", action="store_true", help="Emit metadata plus scout text as JSON.")
    args = parser.parse_args()
    if args.max_input_chars <= 0:
        parser.error("--max-input-chars must be positive")
    if args.max_tokens <= 0:
        parser.error("--max-tokens must be positive")
    return args


def main() -> int:
    args = parse_args()
    timeout = DEEP_TIMEOUT if args.deep and args.timeout == DEFAULT_TIMEOUT else args.timeout
    try:
        report = args.report.expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"error: unable to read report: {exc}", file=sys.stderr)
        return 2
    if "\x00" in report:
        print("error: report contains null bytes; pass a text redacted report", file=sys.stderr)
        return 2

    report = report[: args.max_input_chars]
    payload = {
        "model": args.model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a private local scout for an agent workflow audit. "
                    "This is about local coding-agent workflow friction, not "
                    "application security, web traffic, or threat modeling. "
                    "Do not propose credential-leakage, brute-force, IP-rate, "
                    "bot-traffic, or security-audit work. "
                    "Use only the redacted report. Do not ask for raw traces. "
                    "Give concise, implementation-oriented hypotheses."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Review this redacted rollout-friction analyzer report. "
                    "Identify missed friction classes, likely false positives, "
                    "and concrete analyzer or skill improvements. Group output as: "
                    "Missing Signals, False Positives, Skill Guidance, Validation. "
                    "Stay inside coding-agent workflow mechanics such as helper "
                    "routing, retries, worktrees, validation gates, local config, "
                    "status polling, model-scout boundaries, and trace false "
                    "positives.\n\n"
                    f"{report}"
                ),
            },
        ],
        "temperature": 0.2,
        "max_tokens": args.max_tokens,
        "stream": False,
    }

    try:
        response = post_json(f"{args.base_url.rstrip('/')}/chat/completions", payload, timeout)
    except ScoutError as exc:
        print(f"# Scout unavailable: {exc}")
        print("# Continue with analyzer output and human review; do not retry in a loop.")
        return 1

    content = extract_content(response)
    if not content:
        print("error: LM Studio returned no assistant content", file=sys.stderr)
        return 1
    if args.json:
        print(
            json.dumps(
                {
                    "model": args.model,
                    "timeout_seconds": timeout,
                    "report_chars": len(report),
                    "analysis": content,
                },
                sort_keys=True,
            )
        )
    else:
        print(content)
    return 0


class ScoutError(RuntimeError):
    pass


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
        raise ScoutError(f"request timed out after {timeout:g}s") from exc
    except urllib.error.URLError as exc:
        raise ScoutError(f"request failed: {exc.reason}") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ScoutError(f"response was not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ScoutError("response JSON was not an object")
    if parsed.get("error"):
        raise ScoutError(f"model error: {parsed['error']}")
    return parsed


def extract_content(response: dict[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, str):
        return ""
    return normalize_content(content)


def normalize_content(content: str) -> str:
    content = CHANNEL_RE.sub("", content)
    return content.strip()


if __name__ == "__main__":
    raise SystemExit(main())
