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
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
CHAT_SCRIPT = REPO_ROOT / "local-llm" / "scripts" / "lm_studio_chat.py"
DEFAULT_BASE_URL = None
DEFAULT_MODEL = os.environ.get("ROLLOUT_FRICTION_LM_MODEL")
DEFAULT_ROLE = "rollout_scout"
DEFAULT_TIMEOUT: float | None = None
FALLBACK_PARENT_TIMEOUT = 300
DEEP_TIMEOUT = 180
DEFAULT_MAX_INPUT_CHARS = 12_000
DEFAULT_MAX_TOKENS = 900

CHANNEL_RE = re.compile(r"<\|channel\|>\w+\s*(?:<\|constrain\|>\w+)?\s*<\|message\|>", re.I)
SCOUT_SYSTEM_PROMPT = (
    "You are a private local scout for an agent workflow audit. "
    "This is about local coding-agent workflow friction, not "
    "application security, web traffic, or threat modeling. "
    "Do not propose credential-leakage, brute-force, IP-rate, "
    "bot-traffic, or security-audit work. "
    "Use only the redacted report. Do not ask for raw traces. "
    "Give concise, implementation-oriented hypotheses."
)
SCOUT_USER_PREAMBLE = (
    "Review this redacted rollout-friction analyzer report. "
    "Identify missed friction classes, likely false positives, "
    "and concrete analyzer or skill improvements. Group output as: "
    "Missing Signals, False Positives, Skill Guidance, Validation. "
    "Stay inside coding-agent workflow mechanics such as helper "
    "routing, retries, worktrees, validation gates, local config, "
    "status polling, model-scout boundaries, and trace false "
    "positives."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a bounded local LM Studio scout over a redacted rollout-friction report."
    )
    parser.add_argument("report", type=Path, help="Redacted analyzer report or synthesized notes.")
    parser.add_argument("--config", type=Path, help="Private local-llm config path passed to lm_studio_chat.py.")
    parser.add_argument("--endpoint", help="Endpoint id from private local-llm config.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--role", default=DEFAULT_ROLE, help="Role from local-llm model index/private config.")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Use a longer timeout for deliberate large-model/cold-load scout runs.",
    )
    parser.add_argument("--max-input-chars", type=int, default=DEFAULT_MAX_INPUT_CHARS)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--load-policy", choices=("none", "jit_chat", "api_explicit"), help="Forwarded local-llm lifecycle policy.")
    parser.add_argument("--ttl", type=int, help="Forwarded LM Studio JIT TTL seconds.")
    parser.add_argument("--warmup", action="store_true", help="Run harmless local-llm warmup before the scout prompt.")
    parser.add_argument("--unload-after", action="store_true", help="Unload an explicitly loaded LM Studio instance after chat.")
    parser.add_argument("--json", action="store_true", help="Emit metadata plus scout text as JSON.")
    args = parser.parse_args()
    if args.max_input_chars <= 0:
        parser.error("--max-input-chars must be positive")
    if args.max_tokens <= 0:
        parser.error("--max-tokens must be positive")
    return args


def main() -> int:
    args = parse_args()
    timeout = DEEP_TIMEOUT if args.deep and args.timeout is None else args.timeout
    try:
        report = args.report.expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"error: unable to read report: {exc}", file=sys.stderr)
        return 2
    if "\x00" in report:
        print("error: report contains null bytes; pass a text redacted report", file=sys.stderr)
        return 2

    report = report[: args.max_input_chars]
    try:
        response = run_chat(report, args, timeout)
    except ScoutError as exc:
        print(f"# Scout unavailable: {exc}")
        print("# Continue with analyzer output and human review; do not retry in a loop.")
        return 1

    content = normalize_content(str(response.get("content") or ""))
    if not content:
        print("error: LM Studio returned no assistant content", file=sys.stderr)
        return 1
    if args.json:
        print(
            json.dumps(
                {
                    "model": response.get("model") or args.model,
                    "served_model": response.get("served_model"),
                    "role": response.get("role") or args.role,
                    "endpoint": response.get("endpoint"),
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


def run_chat(report: str, args: argparse.Namespace, timeout: float | None) -> dict[str, Any]:
    if not CHAT_SCRIPT.exists():
        raise ScoutError(f"local-llm chat helper is missing: {CHAT_SCRIPT}")
    prompt = f"{SCOUT_USER_PREAMBLE}\n\n{report}"
    command = [
        "uv",
        "run",
        str(CHAT_SCRIPT),
        "--json",
        "--system",
        SCOUT_SYSTEM_PROMPT,
        "--max-tokens",
        str(args.max_tokens),
    ]
    if timeout is not None:
        command.extend(["--timeout", str(timeout)])
    if args.config:
        command.extend(["--config", str(args.config)])
    if args.endpoint:
        command.extend(["--endpoint", args.endpoint])
    if args.base_url:
        command.extend(["--base-url", args.base_url])
    if args.model:
        command.extend(["--model", args.model])
    elif args.role:
        command.extend(["--role", args.role])
    if args.load_policy:
        command.extend(["--load-policy", args.load_policy])
    if args.ttl:
        command.extend(["--ttl", str(args.ttl)])
    if args.warmup:
        command.append("--warmup")
    if args.unload_after:
        command.append("--unload-after")
    try:
        parent_timeout = timeout + 10 if timeout is not None else FALLBACK_PARENT_TIMEOUT
        completed = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=parent_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        limit = timeout + 10 if timeout is not None else FALLBACK_PARENT_TIMEOUT
        raise ScoutError(f"local-llm chat helper timed out after {limit:g}s") from exc

    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        stderr = completed.stderr.strip()
        detail = f" stderr={stderr}" if stderr else ""
        raise ScoutError(f"local-llm chat helper returned non-JSON output: {exc}{detail}") from exc
    if not isinstance(parsed, dict):
        raise ScoutError("local-llm chat helper JSON was not an object")
    if completed.returncode != 0 or not parsed.get("ok"):
        error = parsed.get("error") or completed.stderr.strip() or f"exit {completed.returncode}"
        raise ScoutError(str(error))
    return parsed


def normalize_content(content: str) -> str:
    content = CHANNEL_RE.sub("", content)
    return content.strip()


if __name__ == "__main__":
    raise SystemExit(main())
