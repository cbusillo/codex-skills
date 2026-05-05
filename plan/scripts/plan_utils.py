#!/usr/bin/env python3
"""Shared helpers for plan scripts."""

from __future__ import annotations

import os
import re
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def get_codex_home() -> Path:
    """Return CODEX_HOME, else the active Code/Codex home."""
    if os.environ.get("CODEX_HOME"):
        return Path(os.environ["CODEX_HOME"]).expanduser()
    code_home = Path("~/.code").expanduser()
    if (code_home / "skills").is_dir() or (code_home / "plans").exists():
        return code_home
    return Path("~/.codex").expanduser()


def get_plans_dir() -> Path:
    return get_codex_home() / "plans"


def validate_plan_name(name: str) -> None:
    if not name or not _NAME_RE.match(name):
        raise ValueError(
            "Invalid plan name. Use short, lower-case, hyphen-delimited names "
            "(e.g., codex-rate-limit-overview)."
        )


def parse_frontmatter(path: Path) -> dict:
    """Parse YAML frontmatter from a markdown file without reading the body."""
    with path.open("r", encoding="utf-8") as handle:
        first = handle.readline()
        if first.strip() != "---":
            raise ValueError("Frontmatter must start with '---'.")

        data: dict[str, str] = {}
        for line in handle:
            stripped = line.strip()
            if stripped == "---":
                return data
            if not stripped or stripped.startswith("#"):
                continue
            if ":" not in line:
                raise ValueError(f"Invalid frontmatter line: {line.rstrip()}")
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if value and len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            data[key] = value

    raise ValueError("Frontmatter must end with '---'.")
