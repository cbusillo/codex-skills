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
MAX_COMMAND_POLICY_ID_LENGTH = 96
MAX_COMMAND_POLICY_MESSAGE_LENGTH = 512
MAX_COMMAND_POLICY_TOKEN_LENGTH = 160
MAX_COMMAND_POLICY_REGEX_LENGTH = 512
MAX_COMMAND_POLICY_PURPOSE_LENGTH = 256
MAX_COMMAND_POLICIES_PER_SKILL = 64
MAX_COMMAND_POLICY_PREFERRED = 8
MAX_COMMAND_POLICY_ARGV_TOKENS = 32
ALLOWED_PROPERTIES = {"name", "description", "metadata", "policy"}
ALLOWED_METADATA_PROPERTIES = {"short-description"}
ALLOWED_POLICY_PROPERTIES = {"allow_implicit_invocation", "command_policies"}
ALLOWED_COMMAND_POLICY_ACTIONS = {"require_preferred", "require_confirm", "reject"}
ALLOWED_COMMAND_POLICY_MATCHERS = {"argv_exact", "argv_prefix", "shell_regex"}
ALLOWED_COMMAND_POLICY_PREFERRED_KINDS = {"script", "skill", "command"}


def validate_skill(skill_path):
    """Basic validation of a skill"""
    skill_path = Path(skill_path)

    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return False, "SKILL.md not found"

    return validate_skill_content(skill_md.read_text(), skill_path)


def validate_skill_content(content, skill_dir=None):
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

    policy = frontmatter.get("policy")
    if policy is not None:
        if not isinstance(policy, dict):
            return False, f"Policy must be a YAML dictionary, got {type(policy).__name__}"
        unexpected_policy_keys = set(policy.keys()) - ALLOWED_POLICY_PROPERTIES
        if unexpected_policy_keys:
            allowed = ", ".join(sorted(ALLOWED_POLICY_PROPERTIES))
            unexpected = ", ".join(sorted(unexpected_policy_keys))
            return (
                False,
                f"Unexpected key(s) in policy: {unexpected}. Allowed policy properties are: {allowed}",
            )

        allow_implicit_invocation = policy.get("allow_implicit_invocation")
        if allow_implicit_invocation is not None and not isinstance(allow_implicit_invocation, bool):
            return (
                False,
                "policy.allow_implicit_invocation must be a boolean, "
                f"got {type(allow_implicit_invocation).__name__}",
            )
        command_policy_error = validate_command_policies(policy.get("command_policies"), skill_dir)
        if command_policy_error:
            return False, command_policy_error

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


def validate_command_policies(command_policies, skill_dir=None):
    if command_policies is None:
        return None
    if not isinstance(command_policies, list):
        return "policy.command_policies must be a list"
    if len(command_policies) > MAX_COMMAND_POLICIES_PER_SKILL:
        return f"policy.command_policies must contain at most {MAX_COMMAND_POLICIES_PER_SKILL} entries"

    for index, command_policy in enumerate(command_policies):
        path = f"policy.command_policies[{index}]"
        if not isinstance(command_policy, dict):
            return f"{path} must be a YAML dictionary"
        unexpected = set(command_policy) - {"id", "match", "action", "message", "preferred"}
        if unexpected:
            return f"Unexpected key(s) in {path}: {', '.join(sorted(unexpected))}"
        if not valid_nonempty_string(command_policy.get("id"), MAX_COMMAND_POLICY_ID_LENGTH):
            return f"{path}.id must be a non-empty string of at most {MAX_COMMAND_POLICY_ID_LENGTH} characters"
        matcher_error = validate_command_policy_match(command_policy.get("match"), path)
        if matcher_error:
            return matcher_error
        action = command_policy.get("action")
        if action not in ALLOWED_COMMAND_POLICY_ACTIONS:
            allowed = ", ".join(sorted(ALLOWED_COMMAND_POLICY_ACTIONS))
            return f"{path}.action must be one of: {allowed}"
        message = command_policy.get("message")
        if message is not None and not valid_nonempty_string(message, MAX_COMMAND_POLICY_MESSAGE_LENGTH):
            return f"{path}.message must be a non-empty string of at most {MAX_COMMAND_POLICY_MESSAGE_LENGTH} characters"
        preferred_error = validate_command_policy_preferred(command_policy.get("preferred"), path, skill_dir)
        if preferred_error:
            return preferred_error
    return None


def validate_command_policy_match(matcher, policy_path):
    path = f"{policy_path}.match"
    if not isinstance(matcher, dict):
        return f"{path} must be a YAML dictionary"
    unexpected = set(matcher) - ALLOWED_COMMAND_POLICY_MATCHERS
    if unexpected:
        return f"Unexpected key(s) in {path}: {', '.join(sorted(unexpected))}"
    present = [key for key in ALLOWED_COMMAND_POLICY_MATCHERS if key in matcher]
    if len(present) != 1:
        return f"{path} must set exactly one of argv_exact, argv_prefix, or shell_regex"
    if "argv_exact" in matcher:
        return validate_argv_tokens(matcher["argv_exact"], f"{path}.argv_exact")
    if "argv_prefix" in matcher:
        return validate_argv_tokens(matcher["argv_prefix"], f"{path}.argv_prefix")
    shell_regex = matcher["shell_regex"]
    if not valid_nonempty_string(shell_regex, MAX_COMMAND_POLICY_REGEX_LENGTH):
        return f"{path}.shell_regex must be a non-empty string of at most {MAX_COMMAND_POLICY_REGEX_LENGTH} characters"
    try:
        re.compile(shell_regex)
    except re.error as exc:
        return f"{path}.shell_regex is invalid: {exc}"
    return None


def validate_command_policy_preferred(preferred, policy_path, skill_dir=None):
    if preferred is None:
        return None
    path = f"{policy_path}.preferred"
    if not isinstance(preferred, list):
        return f"{path} must be a list"
    if len(preferred) > MAX_COMMAND_POLICY_PREFERRED:
        return f"{path} must contain at most {MAX_COMMAND_POLICY_PREFERRED} entries"
    for index, entry in enumerate(preferred):
        entry_path = f"{path}[{index}]"
        if not isinstance(entry, dict):
            return f"{entry_path} must be a YAML dictionary"
        unexpected = set(entry) - {"kind", "path", "name", "example_argv", "purpose"}
        if unexpected:
            return f"Unexpected key(s) in {entry_path}: {', '.join(sorted(unexpected))}"
        kind = entry.get("kind")
        if kind not in ALLOWED_COMMAND_POLICY_PREFERRED_KINDS:
            allowed = ", ".join(sorted(ALLOWED_COMMAND_POLICY_PREFERRED_KINDS))
            return f"{entry_path}.kind must be one of: {allowed}"
        if entry.get("path") is not None:
            script_path = entry["path"]
            if not valid_nonempty_string(script_path, MAX_COMMAND_POLICY_TOKEN_LENGTH):
                return f"{entry_path}.path must be a non-empty string of at most {MAX_COMMAND_POLICY_TOKEN_LENGTH} characters"
            if skill_dir is not None and not (Path(skill_dir) / script_path).exists():
                return f"{entry_path}.path points to missing {script_path}"
        if entry.get("name") is not None and not valid_nonempty_string(entry["name"], MAX_SKILL_NAME_LENGTH):
            return f"{entry_path}.name must be a non-empty string of at most {MAX_SKILL_NAME_LENGTH} characters"
        if entry.get("purpose") is not None and not valid_nonempty_string(entry["purpose"], MAX_COMMAND_POLICY_PURPOSE_LENGTH):
            return f"{entry_path}.purpose must be a non-empty string of at most {MAX_COMMAND_POLICY_PURPOSE_LENGTH} characters"
        argv_error = validate_argv_tokens(entry.get("example_argv", []), f"{entry_path}.example_argv", allow_empty=True)
        if argv_error:
            return argv_error
    return None


def validate_argv_tokens(value, path, allow_empty=False):
    if not isinstance(value, list):
        return f"{path} must be a list"
    if (not allow_empty and not value) or len(value) > MAX_COMMAND_POLICY_ARGV_TOKENS:
        lower = 0 if allow_empty else 1
        return f"{path} must contain between {lower} and {MAX_COMMAND_POLICY_ARGV_TOKENS} tokens"
    for index, token in enumerate(value):
        if not valid_nonempty_string(token, MAX_COMMAND_POLICY_TOKEN_LENGTH):
            return f"{path}[{index}] must be a non-empty string of at most {MAX_COMMAND_POLICY_TOKEN_LENGTH} characters"
    return None


def valid_nonempty_string(value, max_length):
    return isinstance(value, str) and bool(value.strip()) and len(value.strip()) <= max_length


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
        (
            "manual-only-policy",
            "---\nname: demo-skill\ndescription: Use for demo work.\npolicy:\n  allow_implicit_invocation: false\n---\n",
            True,
            "Skill is valid!",
        ),
        (
            "command-policy",
            "---\nname: demo-skill\ndescription: Use for demo work.\npolicy:\n  command_policies:\n    - id: prefer-helper\n      match:\n        argv_prefix: [\"gh\", \"pr\", \"merge\"]\n      action: require_preferred\n      message: Prefer the helper.\n      preferred:\n        - kind: script\n          path: scripts/gh-pr.py\n          example_argv: [\"scripts/gh-pr.py\", \"merge\"]\n          purpose: Use helper.\n---\n",
            True,
            "Skill is valid!",
        ),
        (
            "invalid-policy-type",
            "---\nname: demo-skill\ndescription: Use for demo work.\npolicy:\n  allow_implicit_invocation: no thanks\n---\n",
            False,
            "policy.allow_implicit_invocation must be a boolean",
        ),
        (
            "invalid-command-policy-match",
            "---\nname: demo-skill\ndescription: Use for demo work.\npolicy:\n  command_policies:\n    - id: prefer-helper\n      match:\n        argv_exact: [\"gh\", \"pr\"]\n        argv_prefix: [\"gh\"]\n      action: require_preferred\n---\n",
            False,
            "must set exactly one",
        ),
        (
            "unexpected-policy",
            "---\nname: demo-skill\ndescription: Use for demo work.\npolicy:\n  owner: Code\n---\n",
            False,
            "Unexpected key(s) in policy",
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
