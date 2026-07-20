#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Run Every Code through a configured local/private LLM endpoint."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from lm_studio_api import (
    DEFAULT_CONFIG,
    MODEL_INDEX,
    LocalLLMError,
    load_yaml,
    resolve_endpoint,
    resolve_role,
    role_model,
)


DEFAULT_ROLE = "rollout_scout"
DEFAULT_CODE_BIN = "code"
DEFAULT_CONTEXT_WINDOW = 131_072


class LocalCodeAgentError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run code exec with an isolated local LLM provider config.")
    parser.add_argument("prompt", nargs="?", help="Prompt text. Reads stdin when omitted or '-'.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Private .local/local-llm.yaml path.")
    parser.add_argument("--endpoint", help="Endpoint id from private local-llm config.")
    parser.add_argument("--role", default=DEFAULT_ROLE, help="Model role from local-llm config/model index.")
    parser.add_argument("--model", help="Explicit model id. Overrides --role model selection.")
    parser.add_argument("--base-url", help="Override OpenAI-compatible base URL.")
    parser.add_argument("--code-bin", default=DEFAULT_CODE_BIN, help="Every Code executable to run.")
    parser.add_argument("--workdir", "-C", type=Path, default=Path.cwd(), help="Working root for code exec.")
    parser.add_argument("--sandbox", default="read-only", choices=("read-only", "workspace-write", "danger-full-access"))
    parser.add_argument("--max-seconds", type=int, default=120)
    parser.add_argument("--context-window", type=int, help="Model context window for Code metadata.")
    parser.add_argument("--output-last-message", type=Path, help="Path passed to code exec --output-last-message.")
    parser.add_argument("--json", action="store_true", help="Forward --json to code exec.")
    parser.add_argument("--keep-code-home", type=Path, help="Use and keep this CODE_HOME instead of a temporary one.")
    args = parser.parse_args()
    if args.max_seconds <= 0:
        parser.error("--max-seconds must be positive")
    if args.context_window is not None and args.context_window <= 0:
        parser.error("--context-window must be positive")
    return args


def main() -> int:
    args = parse_args()
    try:
        prompt = read_prompt(args)
        config_text, public_summary = build_code_config(args)
        if args.keep_code_home:
            args.keep_code_home.mkdir(parents=True, exist_ok=True)
            persist_config(args.keep_code_home / "config.toml", config_text)
            return run_code(args, args.keep_code_home, prompt, public_summary)
        with tempfile.TemporaryDirectory(prefix="local-code-agent-") as tmp:
            code_home = Path(tmp)
            persist_config(code_home / "config.toml", config_text)
            return run_code(args, code_home, prompt, public_summary)
    except (LocalCodeAgentError, LocalLLMError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def read_prompt(args: argparse.Namespace) -> str:
    if args.prompt and args.prompt != "-":
        return args.prompt
    return sys.stdin.read()


def build_code_config(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    config = load_yaml(args.config)
    index = load_yaml(MODEL_INDEX)
    role = resolve_role(config, index, args.role)
    endpoint = resolve_endpoint(config, args.endpoint, args.base_url, role)
    if endpoint.get("provider") != "lm_studio":
        raise LocalCodeAgentError("local Code agent currently supports provider=lm_studio endpoints only")
    model = args.model or role_model(role)
    if not model:
        raise LocalCodeAgentError("pass --model or configure a role with a primary model")
    context_window = args.context_window or context_window_from_role(role) or DEFAULT_CONTEXT_WINDOW
    base_url = str(endpoint["base_url"])
    config_text = "\n".join(
        [
            'model_provider = "lmstudio"',
            f"model = {json.dumps(model)}",
            f"model_context_window = {int(context_window)}",
            "",
            "[model_providers.lmstudio]",
            'name = "LM Studio"',
            f"base_url = {json.dumps(base_url)}",
            'wire_api = "chat"',
            "",
        ]
    )
    return config_text, {"provider": "lmstudio", "base_url": base_url, "model": model, "context_window": context_window}


def context_window_from_role(role: dict[str, Any]) -> int | None:
    raw_load = role.get("load")
    load_config = raw_load if isinstance(raw_load, dict) else {}
    for value in (role.get("context_length"), load_config.get("context_length")):
        if value is None:
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def run_code(args: argparse.Namespace, code_home: Path, prompt: str, summary: dict[str, Any]) -> int:
    code_bin = shutil.which(args.code_bin) or args.code_bin
    command = build_code_command(args, code_bin)
    env = os.environ.copy()
    env["CODE_HOME"] = str(code_home)
    print(
        json.dumps(
            {"local_code_agent": summary, "code_home": str(code_home), "command": redacted_command(command)},
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    result = subprocess.run(command, input=prompt, text=True, env=env)
    return result.returncode


def build_code_command(args: argparse.Namespace, code_bin: str) -> list[str]:
    command = [
        code_bin,
        "exec",
        "--skip-git-repo-check",
        "-C",
        str(args.workdir),
        "-s",
        args.sandbox,
        "--max-seconds",
        str(args.max_seconds),
    ]
    if args.json:
        command.append("--json")
    if args.output_last_message:
        command.extend(["--output-last-message", str(args.output_last_message)])
    command.append("-")
    return command


def redacted_command(command: list[str]) -> list[str]:
    return ["<prompt-stdin>" if value == "-" else value for value in command]


def persist_config(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
