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
MAX_STRUCTURED_ITEMS_PER_SKILL = 128
ALLOWED_PROPERTIES = {
    "name",
    "description",
    "metadata",
    "policy",
    "resources",
    "commands",
    "workflow_defaults",
}
ALLOWED_METADATA_PROPERTIES = {"short-description"}
ALLOWED_POLICY_PROPERTIES = {"allow_implicit_invocation", "command_policies"}
ALLOWED_COMMAND_POLICY_ACTIONS = {"require_preferred", "require_confirm", "reject"}
ALLOWED_COMMAND_POLICY_MATCHERS = {"argv_exact", "argv_prefix", "shell_regex"}
ALLOWED_COMMAND_POLICY_PREFERRED_KINDS = {"script", "skill", "command"}
ALLOWED_RESOURCE_KINDS = {"script", "reference", "template", "asset"}
ALLOWED_COMMAND_SOURCES = {"skill", "repo", "external"}


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

    structured_metadata_error = validate_structured_metadata(frontmatter, skill_dir)
    if structured_metadata_error:
        return False, structured_metadata_error

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


def validate_structured_metadata(frontmatter, skill_dir=None):
    resource_paths, resource_error = validate_resources(frontmatter.get("resources"), skill_dir)
    if resource_error:
        return resource_error

    command_error = validate_commands(frontmatter.get("commands"), resource_paths, skill_dir)
    if command_error:
        return command_error

    return validate_workflow_defaults(frontmatter.get("workflow_defaults"))


def validate_resources(resources, skill_dir=None):
    if resources is None:
        return set(), None
    if not isinstance(resources, list):
        return set(), "resources must be a list"
    if len(resources) > MAX_STRUCTURED_ITEMS_PER_SKILL:
        return set(), f"resources must contain at most {MAX_STRUCTURED_ITEMS_PER_SKILL} entries"

    paths = set()
    for index, resource in enumerate(resources):
        path = f"resources[{index}]"
        if not isinstance(resource, dict):
            return paths, f"{path} must be a YAML dictionary"
        unexpected = set(resource) - {"path", "kind", "description"}
        if unexpected:
            return paths, f"Unexpected key(s) in {path}: {', '.join(sorted(unexpected))}"
        raw_path = resource.get("path")
        if not valid_nonempty_string(raw_path, MAX_COMMAND_POLICY_TOKEN_LENGTH):
            return paths, f"{path}.path must be a non-empty string of at most {MAX_COMMAND_POLICY_TOKEN_LENGTH} characters"
        raw_path = str(raw_path).strip()
        path_error = validate_relative_resource_path(raw_path, f"{path}.path")
        if path_error:
            return paths, path_error
        if raw_path in paths:
            return paths, f"{path}.path duplicates {raw_path}"
        paths.add(raw_path)
        if skill_dir is not None and not (Path(skill_dir) / raw_path).is_file():
            return paths, f"{path}.path points to missing file {raw_path}"
        kind = resource.get("kind")
        if not isinstance(kind, str) or kind not in ALLOWED_RESOURCE_KINDS:
            allowed = ", ".join(sorted(ALLOWED_RESOURCE_KINDS))
            return paths, f"{path}.kind must be one of: {allowed}"
        if not valid_nonempty_string(resource.get("description"), MAX_COMMAND_POLICY_PURPOSE_LENGTH):
            return paths, f"{path}.description must be a non-empty string of at most {MAX_COMMAND_POLICY_PURPOSE_LENGTH} characters"
    return paths, None


def validate_commands(commands, resource_paths, skill_dir=None):
    if commands is None:
        return None
    if not isinstance(commands, list):
        return "commands must be a list"
    if len(commands) > MAX_STRUCTURED_ITEMS_PER_SKILL:
        return f"commands must contain at most {MAX_STRUCTURED_ITEMS_PER_SKILL} entries"

    names = set()
    for index, command in enumerate(commands):
        path = f"commands[{index}]"
        if not isinstance(command, dict):
            return f"{path} must be a YAML dictionary"
        unexpected = set(command) - {"name", "source", "resource_path", "example_argv", "purpose"}
        if unexpected:
            return f"Unexpected key(s) in {path}: {', '.join(sorted(unexpected))}"
        name = command.get("name")
        if not valid_nonempty_string(name, MAX_SKILL_NAME_LENGTH):
            return f"{path}.name must be a non-empty string of at most {MAX_SKILL_NAME_LENGTH} characters"
        name = str(name).strip()
        if not re.match(r"^[a-z0-9-]+$", name):
            return f"{path}.name should be hyphen-case (lowercase letters, digits, and hyphens only)"
        if name in names:
            return f"{path}.name duplicates {name}"
        names.add(name)

        if "source" not in command:
            return f"{path}.source is required"
        source = command.get("source")
        if not isinstance(source, str) or source not in ALLOWED_COMMAND_SOURCES:
            allowed = ", ".join(sorted(ALLOWED_COMMAND_SOURCES))
            return f"{path}.source must be one of: {allowed}"

        resource_path = command.get("resource_path")
        if source == "skill":
            if not valid_nonempty_string(resource_path, MAX_COMMAND_POLICY_TOKEN_LENGTH):
                return f"{path}.resource_path must be set for source: skill"
            resource_path = str(resource_path).strip()
            path_error = validate_relative_resource_path(resource_path, f"{path}.resource_path")
            if path_error:
                return path_error
            if resource_path not in resource_paths:
                return f"{path}.resource_path must be listed in resources"
            if skill_dir is not None and not (Path(skill_dir) / resource_path).is_file():
                return f"{path}.resource_path points to missing file {resource_path}"
        elif "resource_path" in command:
            return f"{path}.resource_path is only allowed for source: skill"

        argv_error = validate_argv_tokens(command.get("example_argv"), f"{path}.example_argv")
        if argv_error:
            return argv_error
        if not valid_nonempty_string(command.get("purpose"), MAX_COMMAND_POLICY_PURPOSE_LENGTH):
            return f"{path}.purpose must be a non-empty string of at most {MAX_COMMAND_POLICY_PURPOSE_LENGTH} characters"
    return None


def validate_workflow_defaults(workflow_defaults):
    if workflow_defaults is None:
        return None
    if not isinstance(workflow_defaults, list):
        return "workflow_defaults must be a list"
    if len(workflow_defaults) > MAX_STRUCTURED_ITEMS_PER_SKILL:
        return f"workflow_defaults must contain at most {MAX_STRUCTURED_ITEMS_PER_SKILL} entries"

    names = set()
    for index, workflow_default in enumerate(workflow_defaults):
        path = f"workflow_defaults[{index}]"
        if not isinstance(workflow_default, dict):
            return f"{path} must be a YAML dictionary"
        unexpected = set(workflow_default) - {"name", "value", "description"}
        if unexpected:
            return f"Unexpected key(s) in {path}: {', '.join(sorted(unexpected))}"
        name = workflow_default.get("name")
        if not valid_nonempty_string(name, MAX_SKILL_NAME_LENGTH):
            return f"{path}.name must be a non-empty string of at most {MAX_SKILL_NAME_LENGTH} characters"
        name = str(name).strip()
        if name in names:
            return f"{path}.name duplicates {name}"
        names.add(name)
        if "value" not in workflow_default:
            return f"{path}.value is required"
        if workflow_default["value"] is None:
            return f"{path}.value cannot be null"
        if not valid_nonempty_string(workflow_default.get("description"), MAX_COMMAND_POLICY_PURPOSE_LENGTH):
            return f"{path}.description must be a non-empty string of at most {MAX_COMMAND_POLICY_PURPOSE_LENGTH} characters"
    return None


def validate_relative_resource_path(value, path):
    if value.startswith("/"):
        return f"{path} must be relative"
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return f"{path} must be a normalized relative path"
    return None


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
        if not isinstance(action, str) or action not in ALLOWED_COMMAND_POLICY_ACTIONS:
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
        if not isinstance(kind, str) or kind not in ALLOWED_COMMAND_POLICY_PREFERRED_KINDS:
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
            "structured-metadata",
            "---\nname: demo-skill\ndescription: Use for demo work.\nresources:\n  - path: scripts/helper.py\n    kind: script\n    description: Runs helper.\ncommands:\n  - name: helper\n    source: skill\n    resource_path: scripts/helper.py\n    example_argv: [\"uv\", \"run\", \"scripts/helper.py\"]\n    purpose: Runs helper.\n  - name: repo-check\n    source: repo\n    example_argv: [\"just\", \"check\"]\n    purpose: Runs repo check.\nworkflow_defaults:\n  - name: default_path\n    value: skills\n    description: Default output path.\n---\n",
            True,
            "Skill is valid!",
        ),
        (
            "invalid-resource-kind-type",
            "---\nname: demo-skill\ndescription: Use for demo work.\nresources:\n  - path: scripts/helper.py\n    kind: [script]\n    description: Runs helper.\n---\n",
            False,
            "resources[0].kind must be one of",
        ),
        (
            "invalid-skill-command-resource",
            "---\nname: demo-skill\ndescription: Use for demo work.\nresources:\n  - path: scripts/helper.py\n    kind: script\n    description: Runs helper.\ncommands:\n  - name: missing\n    source: skill\n    resource_path: scripts/missing.py\n    example_argv: [\"uv\", \"run\", \"scripts/missing.py\"]\n    purpose: Runs helper.\n---\n",
            False,
            "resource_path must be listed in resources",
        ),
        (
            "invalid-external-command-resource",
            "---\nname: demo-skill\ndescription: Use for demo work.\ncommands:\n  - name: gh-view\n    source: external\n    resource_path: scripts/helper.py\n    example_argv: [\"gh\", \"pr\", \"view\"]\n    purpose: Views PR.\n---\n",
            False,
            "resource_path is only allowed for source: skill",
        ),
        (
            "invalid-command-source-type",
            "---\nname: demo-skill\ndescription: Use for demo work.\ncommands:\n  - name: gh-view\n    source: [repo]\n    example_argv: [\"gh\", \"pr\", \"view\"]\n    purpose: Views PR.\n---\n",
            False,
            "commands[0].source must be one of",
        ),
        (
            "null-external-command-resource",
            "---\nname: demo-skill\ndescription: Use for demo work.\ncommands:\n  - name: gh-view\n    source: external\n    resource_path: null\n    example_argv: [\"gh\", \"pr\", \"view\"]\n    purpose: Views PR.\n---\n",
            False,
            "resource_path is only allowed for source: skill",
        ),
        (
            "missing-command-source",
            "---\nname: demo-skill\ndescription: Use for demo work.\ncommands:\n  - name: helper\n    example_argv: [\"uv\", \"run\", \"scripts/helper.py\"]\n    purpose: Runs helper.\n---\n",
            False,
            "commands[0].source is required",
        ),
        (
            "invalid-policy-type",
            "---\nname: demo-skill\ndescription: Use for demo work.\npolicy:\n  allow_implicit_invocation: no thanks\n---\n",
            False,
            "policy.allow_implicit_invocation must be a boolean",
        ),
        (
            "invalid-command-policy-action-type",
            "---\nname: demo-skill\ndescription: Use for demo work.\npolicy:\n  command_policies:\n    - id: prefer-helper\n      match:\n        argv_exact: [\"gh\", \"pr\"]\n      action: [require_preferred]\n---\n",
            False,
            "policy.command_policies[0].action must be one of",
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
        (
            "invalid-preferred-kind-type",
            "---\nname: demo-skill\ndescription: Use for demo work.\npolicy:\n  command_policies:\n    - id: prefer-helper\n      match:\n        argv_prefix: [\"gh\", \"pr\"]\n      action: require_preferred\n      preferred:\n        - kind: [script]\n          path: scripts/gh-pr.py\n          example_argv: [\"scripts/gh-pr.py\", \"merge\"]\n          purpose: Use helper.\n---\n",
            False,
            "policy.command_policies[0].preferred[0].kind must be one of",
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

    skill_creator_dir = Path(__file__).resolve().parents[1]
    valid, message = validate_skill_content(
        "---\nname: demo-skill\ndescription: Use for demo work.\nresources:\n  - path: scripts/quick_validate.py\n    kind: script\n    description: Runs helper.\ncommands:\n  - name: helper\n    source: skill\n    resource_path: scripts/quick_validate.py\n    example_argv: [\"uv\", \"run\", \"scripts/quick_validate.py\"]\n    purpose: Runs helper.\n---\n",
        skill_creator_dir,
    )
    if not valid:
        print(f"not ok structured-metadata-existing-file: {message}", file=sys.stderr)
        return 1
    print("ok structured-metadata-existing-file")

    valid, message = validate_skill_content(
        "---\nname: demo-skill\ndescription: Use for demo work.\nresources:\n  - path: scripts\n    kind: script\n    description: Uses a directory.\n---\n",
        skill_creator_dir,
    )
    if valid or "points to missing file scripts" not in message:
        print(
            "not ok structured-metadata-directory-resource: "
            f"got ({valid}, {message!r})",
            file=sys.stderr,
        )
        return 1
    print("ok structured-metadata-directory-resource")

    valid, message = validate_skill_content(
        "---\nname: demo-skill\ndescription: Use for demo work.\nresources:\n  - path: scripts/quick_validate.py\n    kind: script\n    description: Runs helper.\ncommands:\n  - name: helper\n    source: skill\n    resource_path: scripts\n    example_argv: [\"uv\", \"run\", \"scripts\"]\n    purpose: Runs helper.\n---\n",
        skill_creator_dir,
    )
    if valid or "resource_path must be listed in resources" not in message:
        print(
            "not ok structured-metadata-directory-command-resource: "
            f"got ({valid}, {message!r})",
            file=sys.stderr,
        )
        return 1
    print("ok structured-metadata-directory-command-resource")
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
