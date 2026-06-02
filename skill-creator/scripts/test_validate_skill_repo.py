#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML>=6.0.0",
# ]
# ///
"""Focused tests for validate-skill-repo.py."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).with_name("validate-skill-repo.py")


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("validate_skill_repo", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load validate-skill-repo.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def put_text(path: Path, value: str) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(value)


def write_openai_yaml(root: Path, body: str) -> Path:
    skill_dir = root / "demo-skill"
    agents_dir = skill_dir / "agents"
    assets_dir = skill_dir / "assets"
    agents_dir.mkdir(parents=True)
    assets_dir.mkdir()
    put_text(assets_dir / "small.svg", "<svg />")
    put_text(agents_dir / "openai.yaml", body)
    return skill_dir


def test_openai_yaml_accepts_documented_shape() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory(dir=module.ROOT) as tmp:
        root = Path(tmp)
        module.ROOT = root
        skill_dir = write_openai_yaml(
            root,
            """
interface:
  display_name: "Demo Skill"
  short_description: "Demo skill metadata checks"
  icon_small: "./assets/small.svg"
  brand_color: "#3366CC"
  default_prompt: "Use $demo-skill to validate metadata."
dependencies:
  tools:
    - type: "mcp"
      value: "demo"
      description: "Demo MCP server"
      transport: "streamable_http"
      url: "https://example.invalid/mcp"
policy:
  allow_implicit_invocation: false
""".lstrip(),
        )
        errors = module.validate_openai_yaml(skill_dir)
    if errors:
        raise AssertionError(f"documented openai.yaml shape should pass: {errors}")


def test_openai_yaml_rejects_schema_drift() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory(dir=module.ROOT) as tmp:
        root = Path(tmp)
        module.ROOT = root
        skill_dir = write_openai_yaml(
            root,
            """
extra: true
interface:
  display_name: "Demo Skill"
  short_description: "too short"
  default_prompt: "Validate metadata."
  brand_color: "blue"
  unknown: "value"
dependencies:
  tools:
    - type: "http"
      value: "demo"
policy:
  allow_implicit_invocation: "false"
""".lstrip(),
        )
        errors = module.validate_openai_yaml(skill_dir)
    expected_fragments = [
        "unexpected top-level key 'extra'",
        "unexpected interface key 'unknown'",
        "short_description must be 25-64 characters",
        "default_prompt must mention $demo-skill",
        "brand_color must be a #RRGGBB string",
        "dependencies.tools[0].description must be a non-empty string",
        "dependencies.tools[0].type must be 'mcp'",
        "policy.allow_implicit_invocation must be a boolean",
    ]
    for fragment in expected_fragments:
        if not any(fragment in error for error in errors):
            raise AssertionError(f"missing expected error {fragment!r}: {errors}")


def main() -> int:
    test_openai_yaml_accepts_documented_shape()
    test_openai_yaml_rejects_schema_drift()
    print("ok test-validate-skill-repo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
