#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML>=6.0.0",
# ]
# ///
"""
Quick validation script for skills - minimal version
"""

import re
import sys
from pathlib import Path

import yaml

MAX_SKILL_NAME_LENGTH = 64
MAX_SKILL_DESCRIPTION_LENGTH = 1024
ALLOWED_PROPERTIES = {"name", "description", "metadata"}
ALLOWED_METADATA_PROPERTIES = {"short-description"}


def validate_skill(skill_path):
    """Basic validation of a skill"""
    skill_path = Path(skill_path)

    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return False, "SKILL.md not found"

    return validate_skill_content(skill_md.read_text())


def validate_skill_content(content):
    """Validate the contents of a SKILL.md file."""

    if not content.startswith("---"):
        return False, "No YAML frontmatter found"

    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return False, "Invalid frontmatter format"

    frontmatter_text = match.group(1)

    try:
        frontmatter = yaml.safe_load(frontmatter_text)
        if not isinstance(frontmatter, dict):
            return False, "Frontmatter must be a YAML dictionary"
    except yaml.YAMLError as e:
        return False, f"Invalid YAML in frontmatter: {e}"

    unexpected_keys = set(frontmatter.keys()) - ALLOWED_PROPERTIES
    if unexpected_keys:
        allowed = ", ".join(sorted(ALLOWED_PROPERTIES))
        unexpected = ", ".join(sorted(unexpected_keys))
        return (
            False,
            f"Unexpected key(s) in SKILL.md frontmatter: {unexpected}. Allowed properties are: {allowed}",
        )

    metadata = frontmatter.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            return False, f"Metadata must be a YAML dictionary, got {type(metadata).__name__}"
        unexpected_metadata_keys = set(metadata.keys()) - ALLOWED_METADATA_PROPERTIES
        if unexpected_metadata_keys:
            allowed = ", ".join(sorted(ALLOWED_METADATA_PROPERTIES))
            unexpected = ", ".join(sorted(unexpected_metadata_keys))
            return (
                False,
                f"Unexpected key(s) in metadata: {unexpected}. Allowed metadata properties are: {allowed}",
            )

        short_description = metadata.get("short-description")
        if short_description is not None:
            if not isinstance(short_description, str):
                return (
                    False,
                    "metadata.short-description must be a string, "
                    f"got {type(short_description).__name__}",
                )
            short_description = short_description.strip()
            if not short_description:
                return False, "metadata.short-description cannot be empty"
            if len(short_description) > MAX_SKILL_DESCRIPTION_LENGTH:
                return (
                    False,
                    f"metadata.short-description is too long ({len(short_description)} characters). "
                    f"Maximum is {MAX_SKILL_DESCRIPTION_LENGTH} characters.",
                )

    if "name" not in frontmatter:
        return False, "Missing 'name' in frontmatter"
    if "description" not in frontmatter:
        return False, "Missing 'description' in frontmatter"

    name = frontmatter.get("name", "")
    if not isinstance(name, str):
        return False, f"Name must be a string, got {type(name).__name__}"
    name = name.strip()
    if name:
        if not re.match(r"^[a-z0-9-]+$", name):
            return (
                False,
                f"Name '{name}' should be hyphen-case (lowercase letters, digits, and hyphens only)",
            )
        if name.startswith("-") or name.endswith("-") or "--" in name:
            return (
                False,
                f"Name '{name}' cannot start/end with hyphen or contain consecutive hyphens",
            )
        if len(name) > MAX_SKILL_NAME_LENGTH:
            return (
                False,
                f"Name is too long ({len(name)} characters). "
                f"Maximum is {MAX_SKILL_NAME_LENGTH} characters.",
            )

    description = frontmatter.get("description", "")
    if not isinstance(description, str):
        return False, f"Description must be a string, got {type(description).__name__}"
    description = description.strip()
    if description:
        if "<" in description or ">" in description:
            return False, "Description cannot contain angle brackets (< or >)"
        if len(description) > MAX_SKILL_DESCRIPTION_LENGTH:
            return (
                False,
                f"Description is too long ({len(description)} characters). "
                f"Maximum is {MAX_SKILL_DESCRIPTION_LENGTH} characters.",
            )

    return True, "Skill is valid!"


def run_self_tests():
    """Exercise validator policy without relying on repo-local fixtures."""

    cases = [
        (
            "minimal",
            "---\nname: demo-skill\ndescription: Use for demo work.\n---\n\n# Demo\n",
            True,
            "Skill is valid!",
        ),
        (
            "short-description",
            "---\nname: demo-skill\ndescription: Use for demo work.\nmetadata:\n  short-description: Demo work\n---\n",
            True,
            "Skill is valid!",
        ),
        (
            "unexpected-frontmatter",
            "---\nname: demo-skill\ndescription: Use for demo work.\nallowed-tools: Bash\n---\n",
            False,
            "Unexpected key(s) in SKILL.md frontmatter",
        ),
        (
            "unexpected-metadata",
            "---\nname: demo-skill\ndescription: Use for demo work.\nmetadata:\n  owner: Code\n---\n",
            False,
            "Unexpected key(s) in metadata",
        ),
    ]

    for name, content, expected_valid, expected_message in cases:
        valid, message = validate_skill_content(content)
        if valid != expected_valid or expected_message not in message:
            print(
                f"not ok {name}: expected ({expected_valid}, {expected_message!r}), "
                f"got ({valid}, {message!r})",
                file=sys.stderr,
            )
            return 1
        print(f"ok {name}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        sys.exit(run_self_tests())

    if len(sys.argv) != 2:
        print("Usage: python quick_validate.py <skill_directory>|--self-test")
        sys.exit(1)

    valid, message = validate_skill(sys.argv[1])
    print(message)
    sys.exit(0 if valid else 1)
