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


def write_skill_with_sibling_reference(root: Path, referenced_path: str) -> Path:
    skill_dir = root / "demo-skill"
    sibling_scripts_dir = root / "sibling-skill" / "scripts"
    skill_dir.mkdir(parents=True)
    sibling_scripts_dir.mkdir(parents=True)
    put_text(sibling_scripts_dir / "helper.py", "#!/usr/bin/env python3\nprint('ok')\n")
    put_text(
        skill_dir / "SKILL.md",
        f"""
---
name: demo-skill
description: Use for demo work.
---

Use `{referenced_path}` when sibling context is needed.
""".lstrip(),
    )
    return skill_dir


def test_referenced_paths_validate_sibling_skill_paths() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory(dir=module.ROOT) as tmp:
        root = Path(tmp)
        module.ROOT = root
        skill_dir = write_skill_with_sibling_reference(root, "../sibling-skill/scripts/helper.py")
        errors = module.validate_referenced_paths(skill_dir)
    if errors:
        raise AssertionError(f"valid sibling path should pass: {errors}")


def test_referenced_paths_reject_missing_sibling_skill_paths() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory(dir=module.ROOT) as tmp:
        root = Path(tmp)
        module.ROOT = root
        skill_dir = write_skill_with_sibling_reference(root, "../sibling-skill/scripts/missing.py")
        errors = module.validate_referenced_paths(skill_dir)
    if len(errors) != 1 or "references missing ../sibling-skill/scripts/missing.py" not in errors[0]:
        raise AssertionError(f"missing sibling path should fail: {errors}")


def test_markdown_links_validate_relative_targets() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory(dir=module.ROOT) as tmp:
        root = Path(tmp)
        module.ROOT = root
        skill_dir = root / "demo-skill"
        references_dir = skill_dir / "references"
        references_dir.mkdir(parents=True)
        put_text(skill_dir / "SKILL.md", "---\nname: demo-skill\ndescription: Use for demo work.\n---\nSee [guide](references/guide.md).\n")
        put_text(references_dir / "guide.md", "# Guide\n")
        errors = module.validate_markdown_links(skill_dir)
    if errors:
        raise AssertionError(f"valid markdown link should pass: {errors}")


def test_markdown_links_reject_missing_relative_targets() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory(dir=module.ROOT) as tmp:
        root = Path(tmp)
        module.ROOT = root
        skill_dir = root / "demo-skill"
        skill_dir.mkdir(parents=True)
        put_text(skill_dir / "SKILL.md", "---\nname: demo-skill\ndescription: Use for demo work.\n---\nSee [missing](references/missing.md).\n")
        errors = module.validate_markdown_links(skill_dir)
    if len(errors) != 1 or "markdown link target missing: references/missing.md" not in errors[0]:
        raise AssertionError(f"missing markdown link should fail: {errors}")


def test_markdown_links_ignore_fenced_examples_and_root_relative_docs() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory(dir=module.ROOT) as tmp:
        root = Path(tmp)
        module.ROOT = root
        skill_dir = root / "demo-skill"
        skill_dir.mkdir(parents=True)
        put_text(
            skill_dir / "SKILL.md",
            """---
name: demo-skill
description: Use for demo work.
---
See [external docs](/api/docs/guides/latest-model).

```markdown
See [example-only](EXAMPLE.md).
```
""",
        )
        errors = module.validate_markdown_links(skill_dir)
    if errors:
        raise AssertionError(f"fenced/root-relative markdown links should pass: {errors}")


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


def test_command_policy_portability_rejects_installation_identity() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory(dir=module.ROOT) as tmp:
        root = Path(tmp)
        module.ROOT = root
        skill_dir = root / "demo-skill"
        skill_dir.mkdir(parents=True)
        put_text(
            skill_dir / "SKILL.md",
            """
---
name: demo-skill
description: Use for demo work.
policy:
  command_policies:
    - id: prefer-helper
      match:
        argv_prefix: ["demo"]
      action: require_preferred
      message: Use shiny-code-bot for this installation.
      preferred:
        - kind: skill
          name: github
          purpose: Route through shiny-code-bot.
---
""".lstrip(),
        )
        errors = module.validate_command_policy_portability(skill_dir)
    if len(errors) != 2 or not all("installation-specific identity" in error for error in errors):
        raise AssertionError(f"installation identity should fail portability: {errors}")


def test_pep723_metadata_rejects_invalid_toml() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory(dir=module.ROOT) as tmp:
        root = Path(tmp)
        script = root / "helper.py"
        put_text(
            script,
            """#!/usr/bin/env python3
"""
            "# /// "
            """script
# requires-python = ">=3.12"
# dependencies = [
# ///
print("ok")
""",
        )
        errors = module.validate_pep723_metadata(script, script.read_text())
    if len(errors) != 1 or "invalid PEP 723 TOML" not in errors[0]:
        raise AssertionError(f"invalid PEP 723 TOML should fail: {errors}")


def test_pep723_metadata_rejects_multiple_blocks() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory(dir=module.ROOT) as tmp:
        root = Path(tmp)
        script = root / "helper.py"
        put_text(
            script,
            """#!/usr/bin/env python3
"""
            "# /// "
            """script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
            "# /// "
            """script
# requires-python = ">=3.12"
# dependencies = []
# ///
print("ok")
""",
        )
        errors = module.validate_pep723_metadata(script, script.read_text())
    if len(errors) != 1 or "expected exactly one" not in errors[0]:
        raise AssertionError(f"multiple PEP 723 blocks should fail: {errors}")


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

    module = load_module()
    with tempfile.TemporaryDirectory(dir=module.ROOT) as tmp:
        root = Path(tmp)
        module.ROOT = root
        skill_dir = root / "demo-skill"
        sibling_scripts_dir = root / "sibling-skill" / "scripts"
        skill_dir.mkdir(parents=True)
        sibling_scripts_dir.mkdir(parents=True)
        put_text(
            skill_dir / "SKILL.md",
            """
---
name: demo-skill
description: Use for demo work.
policy:
  command_policies:
    - id: prefer-sibling-helper
      match:
        argv_prefix: ["demo"]
      action: require_preferred
      message: Prefer the sibling helper.
      preferred:
        - kind: script
          path: ../sibling-skill/scripts/helper.py
          example_argv: ["uv", "run", "$CODE_HOME/skills/sibling-skill/scripts/helper.py"]
          purpose: Runs the sibling helper.
---
""".lstrip(),
        )
        put_text(
            sibling_scripts_dir / "helper.py",
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
        errors = module.validate_command_example_invocations(skill_dir)
    if errors:
        raise AssertionError(f"uv run sibling PEP 723 examples should pass: {errors}")


def main() -> int:
    test_openai_yaml_accepts_documented_shape()
    test_openai_yaml_rejects_schema_drift()
    test_referenced_paths_validate_sibling_skill_paths()
    test_referenced_paths_reject_missing_sibling_skill_paths()
    test_markdown_links_validate_relative_targets()
    test_markdown_links_reject_missing_relative_targets()
    test_markdown_links_ignore_fenced_examples_and_root_relative_docs()
    test_shell_helper_examples_reject_python_and_uv()
    test_shell_helper_examples_allow_direct_invocation()
    test_pep723_helper_examples_reject_python()
    test_command_policy_portability_rejects_installation_identity()
    test_pep723_metadata_rejects_invalid_toml()
    test_pep723_metadata_rejects_multiple_blocks()
    test_pep723_helper_examples_allow_uv_and_direct_invocation()
    print("ok test-validate-skill-repo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
