#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Check for configured private infra context without printing private values."""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from pathlib import Path
from typing import Any


def runtime_home() -> Path:
    if os.environ.get("CODE_HOME"):
        return Path(os.environ["CODE_HOME"]).expanduser()
    if os.environ.get("CODEX_HOME"):
        return Path(os.environ["CODEX_HOME"]).expanduser()
    return Path.home() / ".code"


DEFAULT_LOCAL_CONTEXT = runtime_home() / "local-context.toml"


def private_context_configured(config: dict[str, Any]) -> bool:
    docs = config.get("docs")
    if not isinstance(docs, dict):
        return False
    value = docs.get("local_infra")
    return isinstance(value, str) and bool(value.strip())


def load_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (FileNotFoundError, PermissionError, tomllib.TOMLDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check whether private infra context is configured."
    )
    parser.add_argument(
        "--local-context",
        type=Path,
        default=DEFAULT_LOCAL_CONTEXT,
        help="local context TOML containing [docs].local_infra",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configured = private_context_configured(load_config(args.local_context))
    print("configured" if configured else "missing")
    return 0 if configured else 1


if __name__ == "__main__":
    raise SystemExit(main())
