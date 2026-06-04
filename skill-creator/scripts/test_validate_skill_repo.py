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


def write_skill_with_shell_helper(root: Path, example_argv: str) -> Path:
    skill_dir = root / "demo-skill"
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    put_text(
        skill_dir / "SKILL.md",
        f"""
---
name: demo-skill
description: Use for demo work.
resources:
  - path: scripts/helper
    kind: script
    description: Runs the shell helper.
commands:
  - name: helper
    source: skill
    resource_path: scripts/helper
    example_argv: {example_argv}
    purpose: Runs the shell helper.
policy:
  command_policies:
    - id: prefer-helper
      match:
        argv_prefix: ["demo"]
      action: require_preferred
      message: Prefer the helper.
      preferred:
        - kind: script
          path: scripts/helper
          example_argv: {example_argv}
          purpose: Runs the shell helper.
---
""".lstrip(),
    )
    put_text(skill_dir / "scripts" / "helper", "#!/usr/bin/env bash\necho ok\n")
    return skill_dir


def write_skill_with_pep723_helper(root: Path, example_argv: str) -> Path:
    skill_dir = root / "demo-skill"
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    put_text(
        skill_dir / "SKILL.md",
        f"""
---
name: demo-skill
description: Use for demo work.
resources:
  - path: scripts/helper.py
    kind: script
    description: Runs the Python helper.
commands:
  - name: helper
    source: skill
    resource_path: scripts/helper.py
    example_argv: {example_argv}
    purpose: Runs the Python helper.
---
""".lstrip(),
    )
    put_text(
        skill_dir / "scripts" / "helper.py",
        """#!/usr/bin/env python3
"""
        "# /// "
        """script
# requires-python = ">=3.12"
# dependencies = []
# ///
print("ok")
""",
    )
    return skill_dir


def test_shell_helper_examples_reject_python_and_uv() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory(dir=module.ROOT) as tmp:
        root = Path(tmp)
        module.ROOT = root
        skill_dir = write_skill_with_shell_helper(root, '["python3", "scripts/helper"]')
        errors = module.validate_command_example_invocations(skill_dir)
    if len(errors) != 2 or not all("not Python" in error for error in errors):
        raise AssertionError(f"shell helper Python examples should fail: {errors}")

    module = load_module()
    with tempfile.TemporaryDirectory(dir=module.ROOT) as tmp:
        root = Path(tmp)
        module.ROOT = root
        skill_dir = write_skill_with_shell_helper(root, '["uv", "run", "scripts/helper"]')
        errors = module.validate_command_example_invocations(skill_dir)
    if len(errors) != 2 or not all("not with uv run" in error for error in errors):
        raise AssertionError(f"shell helper uv examples should fail: {errors}")

    module = load_module()
    with tempfile.TemporaryDirectory(dir=module.ROOT) as tmp:
        root = Path(tmp)
        module.ROOT = root
        skill_dir = write_skill_with_shell_helper(root, '["python", "scripts/helper"]')
        errors = module.validate_command_example_invocations(skill_dir)
    if len(errors) != 2 or not all("not Python" in error for error in errors):
        raise AssertionError(f"shell helper bare Python examples should fail: {errors}")


def test_shell_helper_examples_allow_direct_invocation() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory(dir=module.ROOT) as tmp:
        root = Path(tmp)
        module.ROOT = root
        skill_dir = write_skill_with_shell_helper(root, '["scripts/helper", "--help"]')
        errors = module.validate_command_example_invocations(skill_dir)
    if errors:
        raise AssertionError(f"direct shell helper examples should pass: {errors}")

    module = load_module()
    with tempfile.TemporaryDirectory(dir=module.ROOT) as tmp:
        root = Path(tmp)
        module.ROOT = root
        skill_dir = write_skill_with_shell_helper(root, '["bash", "scripts/helper", "--help"]')
        errors = module.validate_command_example_invocations(skill_dir)
    if errors:
        raise AssertionError(f"bash shell helper examples should pass: {errors}")


def test_pep723_helper_examples_reject_python() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory(dir=module.ROOT) as tmp:
        root = Path(tmp)
        module.ROOT = root
        skill_dir = write_skill_with_pep723_helper(root, '["python3", "scripts/helper.py"]')
        errors = module.validate_command_example_invocations(skill_dir)
    if len(errors) != 1 or "PEP 723 helper" not in errors[0]:
        raise AssertionError(f"PEP 723 Python examples should fail: {errors}")


def test_pep723_helper_examples_allow_uv_and_direct_invocation() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory(dir=module.ROOT) as tmp:
        root = Path(tmp)
        module.ROOT = root
        skill_dir = write_skill_with_pep723_helper(root, '["uv", "run", "scripts/helper.py"]')
        errors = module.validate_command_example_invocations(skill_dir)
    if errors:
        raise AssertionError(f"uv run PEP 723 examples should pass: {errors}")

    module = load_module()
    with tempfile.TemporaryDirectory(dir=module.ROOT) as tmp:
        root = Path(tmp)
        module.ROOT = root
        skill_dir = write_skill_with_pep723_helper(root, '["scripts/helper.py", "--help"]')
        errors = module.validate_command_example_invocations(skill_dir)
    if errors:
        raise AssertionError(f"direct PEP 723 examples should pass: {errors}")


def main() -> int:
    test_openai_yaml_accepts_documented_shape()
    test_openai_yaml_rejects_schema_drift()
    test_shell_helper_examples_reject_python_and_uv()
    test_shell_helper_examples_allow_direct_invocation()
    test_pep723_helper_examples_reject_python()
    test_pep723_helper_examples_allow_uv_and_direct_invocation()
    print("ok test-validate-skill-repo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
