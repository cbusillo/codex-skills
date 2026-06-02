#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML>=6.0.0",
# ]
# ///
"""Repo-wide validation for Every Code skill bundles."""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
IGNORED_SKILL_DIRS = {".disabled", ".git", ".local", ".system", ".code"}
SYSTEM_OVERRIDE_NAMES = {
    "openai-docs",
    "plan",
    "plugin-creator",
    "skill-creator",
    "skill-installer",
}
SYSTEM_SKILLS_MARKER_FILENAME = ".codex-system-skills.marker"
LOCAL_PATH_RE = re.compile(r"`((?:scripts|references|assets)/[^`\s]+)`")
SKILL_CREATOR_REF_RE = re.compile(r"<path-to-skill-creator>/scripts/([^`\s]+)")
ALLOWED_OPENAI_INTERFACE_KEYS = {
    "display_name",
    "short_description",
    "icon_small",
    "icon_large",
    "brand_color",
    "default_prompt",
}
ALLOWED_OPENAI_TOP_LEVEL_KEYS = {"interface", "dependencies", "policy"}
HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
EXAMPLE_MARKERS = (
    "**example",
    "**examples",
    "for example",
    "examples:",
    "example:",
    "would be helpful",
)


def load_quick_validate() -> Any:
    path = Path(__file__).with_name("quick_validate.py")
    spec = importlib.util.spec_from_file_location("quick_validate_under_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


quick_validate = load_quick_validate()


def active_skill_dirs() -> list[Path]:
    dirs: list[Path] = []
    for skill_md in sorted(ROOT.glob("*/SKILL.md")):
        if skill_md.parts[-2] in IGNORED_SKILL_DIRS:
            continue
        dirs.append(skill_md.parent)
    return dirs


def system_skill_names() -> set[str]:
    system_root = resolve_system_skills_root()
    names: set[str] = set()
    for skill_md in sorted(system_root.glob("*/SKILL.md")):
        frontmatter = read_frontmatter(skill_md)
        name = frontmatter.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def validate_system_override_paths(skill_dirs: list[Path]) -> list[str]:
    errors: list[str] = []
    active_by_name = {skill_dir.name: skill_dir for skill_dir in skill_dirs}
    for name in sorted(SYSTEM_OVERRIDE_NAMES & active_by_name.keys()):
        local_skill = active_by_name[name] / "SKILL.md"
        if not local_skill.is_file():
            errors.append(f"{name}: override skill is missing {local_skill.relative_to(ROOT)}")
    return errors


def resolve_system_skills_root() -> Path:
    """Return the Every Code runtime system skill cache or the repo fallback.

    Every Code caches embedded system skills under the active runtime skills directory:
    `CODE_HOME/skills/.system` for Every Code, with `CODEX_HOME/skills/.system` kept
    for compatibility. This repo may also contain a generated `.system` cache,
    which keeps override-name validation deterministic for plain checkouts and
    CI jobs that do not mount a runtime cache.
    """

    for candidate in runtime_system_root_candidates():
        if is_system_skills_root(candidate):
            return candidate
    return ROOT / ".system"


def runtime_system_root_candidates() -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()
    for home in runtime_home_candidates():
        candidate = (home / "skills" / ".system").expanduser().resolve()
        if candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)
    return candidates


def runtime_home_candidates() -> list[Path]:
    candidates: list[Path] = []
    code_home = os.environ.get("CODE_HOME", "").strip()
    if code_home:
        candidates.append(Path(code_home))
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        candidates.append(Path(codex_home))
    home = Path.home()
    candidates.extend((home / ".code", home / ".codex"))
    return candidates


def is_system_skills_root(path: Path) -> bool:
    return (path / SYSTEM_SKILLS_MARKER_FILENAME).is_file() and any(
        path.glob("*/SKILL.md")
    )


def read_frontmatter(skill_md: Path) -> dict[str, Any]:
    contents = skill_md.read_text()
    match = re.match(r"^---\n(.*?)\n---", contents, re.DOTALL)
    if not match:
        return {}
    parsed = yaml.safe_load(match.group(1))
    return parsed if isinstance(parsed, dict) else {}


def validate_openai_yaml(skill_dir: Path) -> list[str]:
    path = skill_dir / "agents" / "openai.yaml"
    if not path.exists():
        return []

    errors: list[str] = []
    try:
        parsed = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        return [f"{path.relative_to(ROOT)}: invalid YAML: {exc}"]

    if not isinstance(parsed, dict):
        return [f"{path.relative_to(ROOT)}: must be a YAML mapping"]

    for key in parsed:
        if key not in ALLOWED_OPENAI_TOP_LEVEL_KEYS:
            errors.append(f"{path.relative_to(ROOT)}: unexpected top-level key {key!r}")

    interface = parsed.get("interface")
    if interface is not None and not isinstance(interface, dict):
        errors.append(f"{path.relative_to(ROOT)}: interface must be a mapping")
    elif isinstance(interface, dict):
        for key in interface:
            if key not in ALLOWED_OPENAI_INTERFACE_KEYS:
                errors.append(f"{path.relative_to(ROOT)}: unexpected interface key {key!r}")
        for key in ("display_name", "short_description"):
            value = interface.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{path.relative_to(ROOT)}: interface.{key} must be a non-empty string")
        short_description = interface.get("short_description")
        if isinstance(short_description, str) and not (25 <= len(short_description) <= 64):
            errors.append(
                f"{path.relative_to(ROOT)}: interface.short_description must be 25-64 characters (got {len(short_description)})"
            )
        default_prompt = interface.get("default_prompt")
        if default_prompt is not None:
            if not isinstance(default_prompt, str) or not default_prompt.strip():
                errors.append(f"{path.relative_to(ROOT)}: interface.default_prompt must be a non-empty string")
            elif f"${skill_dir.name}" not in default_prompt:
                errors.append(
                    f"{path.relative_to(ROOT)}: interface.default_prompt must mention ${skill_dir.name}"
                )
        brand_color = interface.get("brand_color")
        if brand_color is not None and (not isinstance(brand_color, str) or not HEX_COLOR_RE.match(brand_color)):
            errors.append(f"{path.relative_to(ROOT)}: interface.brand_color must be a #RRGGBB string")
        for key in ("icon_small", "icon_large"):
            value = interface.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                errors.append(f"{path.relative_to(ROOT)}: interface.{key} must be a string")
                continue
            asset_path = skill_dir / value
            if not asset_path.exists():
                errors.append(
                    f"{path.relative_to(ROOT)}: interface.{key} points to missing {value}"
                )

    policy = parsed.get("policy")
    if policy is not None and not isinstance(policy, dict):
        errors.append(f"{path.relative_to(ROOT)}: policy must be a mapping")
    elif isinstance(policy, dict):
        for key, value in policy.items():
            if key != "allow_implicit_invocation":
                errors.append(f"{path.relative_to(ROOT)}: unexpected policy key {key!r}")
            elif not isinstance(value, bool):
                errors.append(f"{path.relative_to(ROOT)}: policy.allow_implicit_invocation must be a boolean")

    dependencies = parsed.get("dependencies")
    if dependencies is not None and not isinstance(dependencies, dict):
        errors.append(f"{path.relative_to(ROOT)}: dependencies must be a mapping")
    elif isinstance(dependencies, dict):
        errors.extend(validate_openai_dependencies(path, dependencies))

    return errors


def validate_openai_dependencies(path: Path, dependencies: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in dependencies:
        if key != "tools":
            errors.append(f"{path.relative_to(ROOT)}: unexpected dependencies key {key!r}")
    tools = dependencies.get("tools")
    if tools is None:
        return errors
    if not isinstance(tools, list):
        errors.append(f"{path.relative_to(ROOT)}: dependencies.tools must be a list")
        return errors
    for index, tool in enumerate(tools):
        prefix = f"{path.relative_to(ROOT)}: dependencies.tools[{index}]"
        if not isinstance(tool, dict):
            errors.append(f"{prefix} must be a mapping")
            continue
        for key in tool:
            if key not in {"type", "value", "description", "transport", "url"}:
                errors.append(f"{prefix} has unexpected key {key!r}")
        for key in ("type", "value", "description"):
            value = tool.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{prefix}.{key} must be a non-empty string")
        if tool.get("type") != "mcp":
            errors.append(f"{prefix}.type must be 'mcp'")
        for key in ("transport", "url"):
            value = tool.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                errors.append(f"{prefix}.{key} must be a non-empty string when present")
    return errors


def validate_skill_command_policy_paths(skill_dir: Path) -> list[str]:
    frontmatter = read_frontmatter(skill_dir / "SKILL.md")
    policy = frontmatter.get("policy")
    if not isinstance(policy, dict):
        return []
    command_policies = policy.get("command_policies")
    if not isinstance(command_policies, list):
        return []

    errors: list[str] = []
    skill_md = skill_dir / "SKILL.md"
    for policy_index, command_policy in enumerate(command_policies):
        if not isinstance(command_policy, dict):
            continue
        preferred = command_policy.get("preferred")
        if not isinstance(preferred, list):
            continue
        for preferred_index, entry in enumerate(preferred):
            if not isinstance(entry, dict):
                continue
            raw = entry.get("path")
            if not isinstance(raw, str) or not raw.strip():
                continue
            if not (skill_dir / raw).exists():
                errors.append(
                    f"{skill_md.relative_to(ROOT)}: policy.command_policies[{policy_index}].preferred[{preferred_index}].path points to missing {raw}"
                )
    return errors


def validate_referenced_paths(skill_dir: Path) -> list[str]:
    skill_md = skill_dir / "SKILL.md"
    lines = skill_md.read_text().splitlines()
    errors: list[str] = []

    for line in lines:
        normalized_line = line.strip().lower()
        if any(marker in normalized_line for marker in EXAMPLE_MARKERS):
            continue

        for match in LOCAL_PATH_RE.finditer(line):
            raw = match.group(1).rstrip(".,);]")
            if "<" in raw or ">" in raw:
                continue
            candidate = skill_dir / raw
            if not candidate.exists():
                errors.append(f"{skill_md.relative_to(ROOT)}: references missing {raw}")

        for match in SKILL_CREATOR_REF_RE.finditer(line):
            raw = f"scripts/{match.group(1).rstrip('.,);]')}"
            candidate = ROOT / "skill-creator" / raw
            if not candidate.exists():
                errors.append(
                    f"{skill_md.relative_to(ROOT)}: references missing skill-creator/{raw}"
                )

    return errors


def validate_python_script_metadata(skill_dir: Path) -> list[str]:
    errors: list[str] = []
    scripts_dir = skill_dir / "scripts"
    if not scripts_dir.exists():
        return errors

    for script in sorted(scripts_dir.glob("*.py")):
        text = script.read_text()
        if "# /// script" not in text:
            errors.append(f"{script.relative_to(ROOT)}: missing PEP 723 script metadata")
    return errors


def validate_skill_dir(skill_dir: Path) -> list[str]:
    errors: list[str] = []
    valid, message = quick_validate.validate_skill(skill_dir)
    if not valid:
        errors.append(f"{(skill_dir / 'SKILL.md').relative_to(ROOT)}: {message}")

    frontmatter = read_frontmatter(skill_dir / "SKILL.md")
    name = frontmatter.get("name")
    if isinstance(name, str) and name != skill_dir.name:
        errors.append(
            f"{(skill_dir / 'SKILL.md').relative_to(ROOT)}: name {name!r} does not match directory {skill_dir.name!r}"
        )

    errors.extend(validate_referenced_paths(skill_dir))
    errors.extend(validate_skill_command_policy_paths(skill_dir))
    errors.extend(validate_openai_yaml(skill_dir))
    errors.extend(validate_python_script_metadata(skill_dir))
    return errors


def main() -> int:
    errors: list[str] = []
    skill_dirs = active_skill_dirs()
    if not skill_dirs:
        errors.append("no active top-level skills found")

    for skill_dir in skill_dirs:
        errors.extend(validate_skill_dir(skill_dir))
    errors.extend(validate_system_override_paths(skill_dirs))

    active_names = {skill_dir.name for skill_dir in skill_dirs}
    overlapping_system_names = active_names & system_skill_names()
    unexpected_overrides = overlapping_system_names - SYSTEM_OVERRIDE_NAMES
    for name in sorted(unexpected_overrides):
        errors.append(
            f"{name}: active skill overrides .system/{name} but is not in SYSTEM_OVERRIDE_NAMES"
        )

    if errors:
        for error in errors:
            print(f"not ok {error}", file=sys.stderr)
        return 1

    print(f"ok validate-skill-repo ({len(skill_dirs)} active skills)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
