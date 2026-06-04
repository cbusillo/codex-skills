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
    (["gh", "pr", "merge", "123"], None, "github", "prefer-gh-pr-merge-helper"),
    (["gh", "issue", "create"], None, "github", "prefer-gh-issue-create-helper"),
    (["gh", "issue", "list", "--label", "plan"], None, "github-plan", "prefer-gh-plan-index-for-issue-list"),
    (["gh", "search", "issues", "repo:owner/repo"], None, "github-plan", "prefer-gh-plan-search-for-issue-search"),
    (["gh", "project", "item-list", "1"], None, "github-plan", "prefer-gh-plan-helper-for-project-commands"),
    (["gh", "api", "graphql"], graphql_shell(), "github-plan", "prefer-gh-plan-helper-for-planning-graphql"),
    (["curl", "inspection"], inspection_shell(), "jetbrains-inspection", "prefer-jb-inspect-for-plugin-http"),
    (["curl", "launchplane-apply"], launchplane_apply_shell(), "launchplane", "prefer-launchplane-write-helper-for-product-config-api"),
    (["launchplane", "merge-train", "run-once"], None, "launchplane", "prefer-launchplane-helpers-over-global-cli"),
)


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
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="*", help="Command tokens to simulate")
    parser.add_argument("--shell", help="Shell string to use for shell_regex matching")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

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
