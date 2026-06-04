#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML>=6.0.0",
# ]
# ///
"""Simulate command policy matching across active skills."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
IGNORED_SKILL_DIRS = {".disabled", ".git", ".local", ".system", ".code"}


@dataclass(frozen=True)
class PolicyMatch:
    skill: str
    policy_id: str
    matcher: str
    score: tuple[int, int, int, int]


def active_skill_dirs() -> list[Path]:
    return [
        path.parent
        for path in sorted(ROOT.glob("*/SKILL.md"))
        if path.parts[-2] not in IGNORED_SKILL_DIRS
    ]


def read_frontmatter(skill_md: Path) -> dict[str, Any]:
    text = skill_md.read_text()
    match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return {}
    parsed = yaml.safe_load(match.group(1))
    return parsed if isinstance(parsed, dict) else {}


def iter_policies() -> list[tuple[int, str, int, dict[str, Any]]]:
    policies: list[tuple[int, str, int, dict[str, Any]]] = []
    for skill_order, skill_dir in enumerate(active_skill_dirs()):
        frontmatter = read_frontmatter(skill_dir / "SKILL.md")
        command_policies = frontmatter.get("policy", {}).get("command_policies", [])
        if not isinstance(command_policies, list):
            continue
        for index, policy in enumerate(command_policies):
            if isinstance(policy, dict):
                policies.append((skill_order, skill_dir.name, index, policy))
    return policies


def match_policy(
    skill_order: int,
    skill: str,
    index: int,
    policy: dict[str, Any],
    argv: list[str],
    shell: str,
) -> PolicyMatch | None:
    matcher = policy.get("match")
    if not isinstance(matcher, dict):
        return None
    policy_id = str(policy.get("id") or f"policy-{index}")
    if "argv_exact" in matcher:
        expected = [str(token) for token in matcher["argv_exact"]]
        if argv == expected:
            return PolicyMatch(skill, policy_id, "argv_exact", (3, len(expected), -skill_order, -index))
        return None
    if "argv_prefix" in matcher:
        expected = [str(token) for token in matcher["argv_prefix"]]
        if argv[: len(expected)] == expected:
            return PolicyMatch(skill, policy_id, "argv_prefix", (2, len(expected), -skill_order, -index))
        return None
    if "shell_regex" in matcher and re.search(str(matcher["shell_regex"]), shell):
        return PolicyMatch(skill, policy_id, "shell_regex", (1, 0, -skill_order, -index))
    return None


def simulate(argv: list[str], shell: str | None = None) -> list[PolicyMatch]:
    shell_text = shell if shell is not None else shlex.join(argv)
    matches = [
        match
        for skill_order, skill, index, policy in iter_policies()
        if (match := match_policy(skill_order, skill, index, policy, argv, shell_text)) is not None
    ]
    return sorted(matches, key=lambda match: match.score, reverse=True)


def primary_match(argv: list[str], shell: str | None = None) -> PolicyMatch | None:
    matches = simulate(argv, shell)
    return matches[0] if matches else None


def graphql_shell() -> str:
    mutation = "update" + "ProjectV2ItemFieldValue"
    return " ".join(("gh", "api", "graphql", "-f", f"query=mutation {{ {mutation}(input:{{}}) {{ clientMutationId }} }}"))


def inspection_shell() -> str:
    return " ".join(("curl", "http://127.0.0.1:63342/api/" + "inspection/problems"))


def launchplane_apply_shell() -> str:
    return " ".join(("curl", "https://launchplane.example.invalid/v1/product-config/" + "apply"))


EXPECTATIONS: tuple[tuple[list[str], str | None, str, str], ...] = (
    (["gh", "pr", "create", "--title", "demo"], None, "github", "prefer-gh-pr-create-helper"),
    (["gh", "pr", "edit", "123", "--body-file", "body.md"], None, "github", "prefer-gh-pr-edit-helper"),
    (["gh", "pr", "comment", "123", "--body-file", "body.md"], None, "github", "prefer-gh-pr-comment-helper"),
    (["gh", "pr", "merge", "123"], None, "github", "prefer-gh-pr-merge-helper"),
    (["gh", "issue", "create"], None, "github", "prefer-gh-issue-create-helper"),
    (["gh", "issue", "edit", "123"], None, "github", "prefer-gh-issue-edit-helper"),
    (["gh", "issue", "close", "123"], None, "github", "prefer-gh-issue-close-helper"),
    (["gh", "issue", "list", "--label", "plan"], None, "github-plan", "prefer-gh-plan-index-for-issue-list"),
    (["gh", "search", "issues", "repo:owner/repo"], None, "github-plan", "prefer-gh-plan-search-for-issue-search"),
    (["gh", "project", "item-list", "1"], None, "github-plan", "prefer-gh-plan-helper-for-project-commands"),
    (["gh", "api", "graphql"], graphql_shell(), "github-plan", "prefer-gh-plan-helper-for-planning-graphql"),
    (["curl", "inspection"], inspection_shell(), "jetbrains-inspection", "prefer-jb-inspect-for-plugin-http"),
    (["curl", "launchplane-apply"], launchplane_apply_shell(), "launchplane", "prefer-launchplane-write-helper-for-product-config-api"),
    (["launchplane", "merge-train", "run-once"], None, "launchplane", "prefer-launchplane-helpers-over-global-cli"),
)

NEGATIVE_EXPECTATIONS: tuple[tuple[list[str], str | None], ...] = (
    (["gh", "issue", "view", "123"], None),
    (["gh", "api", "graphql"], "gh api graphql -f query='{ viewer { login } }'"),
    (["curl", "https://example.invalid/health"], None),
)


def precedence_self_test() -> list[str]:
    policies = [
        (
            0,
            "alpha",
            0,
            {
                "id": "shell-match",
                "match": {"shell_regex": r"\bdemo\b"},
            },
        ),
        (
            0,
            "alpha",
            1,
            {
                "id": "prefix-short",
                "match": {"argv_prefix": ["demo"]},
            },
        ),
        (
            1,
            "beta",
            0,
            {
                "id": "prefix-long",
                "match": {"argv_prefix": ["demo", "run"]},
            },
        ),
        (
            2,
            "gamma",
            0,
            {
                "id": "exact",
                "match": {"argv_exact": ["demo", "run", "now"]},
            },
        ),
    ]

    matches = [
        match
        for skill_order, skill, index, policy in policies
        if (match := match_policy(skill_order, skill, index, policy, ["demo", "run", "now"], "demo run now"))
        is not None
    ]
    ordered = [match.policy_id for match in sorted(matches, key=lambda match: match.score, reverse=True)]
    if ordered != ["exact", "prefix-long", "prefix-short", "shell-match"]:
        return [f"policy precedence order drifted: {ordered}"]
    return []


def validate_expectations() -> list[str]:
    errors: list[str] = []
    for argv, shell, expected_skill, expected_policy in EXPECTATIONS:
        match = primary_match(argv, shell)
        if match is None:
            errors.append(f"{shlex.join(argv)}: expected {expected_skill}/{expected_policy}, got no match")
            continue
        if match.skill != expected_skill or match.policy_id != expected_policy:
            errors.append(
                f"{shlex.join(argv)}: expected {expected_skill}/{expected_policy}, got {match.skill}/{match.policy_id}"
            )
    for argv, shell in NEGATIVE_EXPECTATIONS:
        match = primary_match(argv, shell)
        if match is not None:
            errors.append(f"{shlex.join(argv)}: expected no match, got {match.skill}/{match.policy_id}")
    errors.extend(precedence_self_test())
    return errors


def self_test() -> None:
    global iter_policies
    original_iter = iter_policies

    # Test case 1: Matcher precedence (argv_exact beats argv_prefix beats shell_regex)
    mock_policies = [
        (0, "skill-a", 0, {"id": "exact-policy", "match": {"argv_exact": ["demo"]}}),
        (1, "skill-b", 0, {"id": "prefix-policy", "match": {"argv_prefix": ["demo"]}}),
        (2, "skill-c", 0, {"id": "regex-policy", "match": {"shell_regex": "demo"}}),
    ]
    iter_policies = lambda: mock_policies
    matches = simulate(["demo"])
    assert len(matches) == 3
    assert matches[0].skill == "skill-a"
    assert matches[0].policy_id == "exact-policy"
    assert matches[1].skill == "skill-b"
    assert matches[2].skill == "skill-c"

    # Test case 2: Prefix length precedence (longer prefix beats shorter prefix)
    mock_policies = [
        (0, "skill-a", 0, {"id": "short-prefix", "match": {"argv_prefix": ["demo"]}}),
        (1, "skill-b", 0, {"id": "long-prefix", "match": {"argv_prefix": ["demo", "sub"]}}),
    ]
    iter_policies = lambda: mock_policies
    matches = simulate(["demo", "sub"])
    assert len(matches) == 2
    assert matches[0].skill == "skill-b"
    assert matches[0].policy_id == "long-prefix"

    # Test case 3: Skill order tie-breaking (alphabetical/list order)
    mock_policies = [
        (1, "skill-b", 0, {"id": "policy-b", "match": {"argv_prefix": ["demo"]}}),
        (0, "skill-a", 0, {"id": "policy-a", "match": {"argv_prefix": ["demo"]}}),
    ]
    iter_policies = lambda: mock_policies
    matches = simulate(["demo"])
    assert len(matches) == 2
    assert matches[0].skill == "skill-a"

    # Test case 4: Policy index tie-breaking (index order)
    mock_policies = [
        (0, "skill-a", 1, {"id": "policy-second", "match": {"argv_prefix": ["demo"]}}),
        (0, "skill-a", 0, {"id": "policy-first", "match": {"argv_prefix": ["demo"]}}),
    ]
    iter_policies = lambda: mock_policies
    matches = simulate(["demo"])
    assert len(matches) == 2
    assert matches[0].policy_id == "policy-first"

    iter_policies = original_iter
    print("ok validate-command-policy-simulator self-test")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="*", help="Command tokens to simulate")
    parser.add_argument("--shell", help="Shell string to use for shell_regex matching")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--self-test", action="store_true", help="Run precedence self tests")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    if args.command:
        payload = [match.__dict__ for match in simulate(args.command, args.shell)]
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            for match in payload:
                print(f"{match['skill']}\t{match['policy_id']}\t{match['matcher']}")
        return 0

    errors = validate_expectations()
    if errors:
        for error in errors:
            print(f"not ok {error}", file=sys.stderr)
        return 1
    print("ok validate-command-policy-simulator")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
