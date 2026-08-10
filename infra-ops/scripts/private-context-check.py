#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Check for configured private infra context without printing private values."""

from __future__ import annotations

import argparse
import os
import tomllib
from pathlib import Path
from typing import Any, Mapping


LOCAL_CONTEXT_FILENAME = "local-context.toml"


def local_context_candidates(
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[Path]:
    environment = os.environ if environment is None else environment
    candidates: list[Path] = []
    for variable in ("CODE_HOME", "CODEX_HOME"):
        raw_home = environment.get(variable, "").strip()
        if raw_home:
            candidate = Path(raw_home).expanduser() / LOCAL_CONTEXT_FILENAME
            if candidate not in candidates:
                candidates.append(candidate)

    default_home = Path.home() if home is None else home
    default_candidate = default_home / ".code" / LOCAL_CONTEXT_FILENAME
    if default_candidate not in candidates:
        candidates.append(default_candidate)
    return candidates


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
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def resolve_local_context(
    explicit_path: Path | None,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path | None:
    if explicit_path is not None:
        configured = private_context_configured(load_config(explicit_path))
        return explicit_path if configured else None
    for candidate in local_context_candidates(environment, home):
        if private_context_configured(load_config(candidate)):
            return candidate
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check whether private infra context is configured."
    )
    parser.add_argument(
        "--local-context",
        type=Path,
        help=(
            "explicit local context TOML containing [docs].local_infra; "
            "otherwise discover CODE_HOME, CODEX_HOME, then ~/.code"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configured = resolve_local_context(args.local_context) is not None
    print("configured" if configured else "missing")
    return 0 if configured else 1


if __name__ == "__main__":
    raise SystemExit(main())
