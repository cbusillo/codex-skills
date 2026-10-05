#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Simulate command policy matching across active skills."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

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


def policy_catalog() -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for skill_order, skill, index, policy in iter_policies():
        matcher = policy.get("match")
        if not isinstance(matcher, dict):
            continue
        preferred = policy.get("preferred")
        if not isinstance(preferred, list):
            preferred = []
        catalog.append(
            {
                "skill": skill,
                "skill_order": skill_order,
                "policy_index": index,
                "id": str(policy.get("id") or f"policy-{index}"),
                "action": policy.get("action"),
                "match": matcher,
                "preferred": preferred,
                "message": policy.get("message"),
                "exceptions": policy.get("exceptions", []),
            }
        )
    return catalog


def match_policy(
    skill_order: int,
    skill: str,
    index: int,
    policy: dict[str, Any],
    argv: list[str],
    shell: str,
    repository: str | None = None,
    command_texts: list[str] | None = None,
) -> PolicyMatch | None:
    matcher = policy.get("match")
    if not isinstance(matcher, dict):
        return None
    for exception in policy.get("exceptions", []):
        if repository is not None and exception.get("repository") == repository:
            prefix = exception.get("argv_prefix", [])
            if prefix and argv[:len(prefix)] == prefix:
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
    if "shell_regex" in matcher:
        pattern = re.compile(str(matcher["shell_regex"]))
        if command_texts is None:
            matched = pattern.search(shell) is not None
        else:
            matched = any(pattern.match(text) for text in command_texts)
        if matched:
            return PolicyMatch(skill, policy_id, "shell_regex", (1, 0, -skill_order, -index))
    return None


def verified_repository(cwd: Path) -> str | None:
    """Identify a GitHub checkout/worktree from its own origin, without network I/O."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            env=environment, capture_output=True, text=True, timeout=2, check=True,
        )
        root = result.stdout.strip()
        result = subprocess.run(
            ["git", "-C", root, "config", "--get", "remote.origin.url"],
            env=environment, capture_output=True, text=True, timeout=2, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", result.stdout.strip(),
    )
    return match.group(1).lower() if match else None


def command_position_texts(argv: list[str]) -> list[str]:
    """The command's text from each token on, so a regex can start only where a token starts.

    Quoting keeps a data argument such as `rg 'gh api'` from matching, while
    `sudo gh api` or `xargs gh api` still match at the `gh` token.
    """
    return [shlex.join(argv[start:]) for start in range(len(argv))]


def simulate(
    argv: list[str],
    shell: str | None = None,
    *,
    cwd: Path | None = None,
    command_argv: list[str] | None = None,
) -> list[PolicyMatch]:
    """Match `argv` against every policy.

    `shell_regex` searches `shell` (default: the joined argv). Given
    `command_argv`, the one simple command a shell line runs, a regex instead
    matches only from command position in it.
    """
    shell_text = shell if shell is not None else shlex.join(argv)
    command_texts = command_position_texts(command_argv) if command_argv is not None else None
    policies = iter_policies()
    needs_repository = any(
        argv[:len(exception["argv_prefix"])] == exception["argv_prefix"]
        for _, _, _, policy in policies for exception in policy.get("exceptions", [])
    )
    repository = verified_repository(cwd) if cwd is not None and needs_repository else None
    matches = [
        match
        for skill_order, skill, index, policy in policies
        if (match := match_policy(skill_order, skill, index, policy, argv, shell_text, repository, command_texts)) is not None
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


def launchplane_merge_train_shell() -> str:
    return " ".join(
        (
            "curl",
            "https://launchplane.example.invalid/v1/work-graph/merge-train/controller/run-once",
        )
    )


# Which skill intercepts each command. Policy ids are frontmatter literals and
# may be renamed freely, so they are not asserted here.
EXPECTATIONS: tuple[tuple[list[str], str | None, str], ...] = (
    (["gh", "pr", "create", "--title", "demo"], None, "github"),
    (["gh", "pr", "edit", "123", "--body-file", "body.md"], None, "github"),
    (["gh", "pr", "comment", "123", "--body-file", "body.md"], None, "github"),
    (["gh", "pr", "review", "123", "--comment"], None, "github"),
    (["gh", "pr", "close", "123"], None, "github"),
    (["gh", "pr", "reopen", "123"], None, "github"),
    (["gh", "pr", "ready", "123"], None, "github"),
    (["gh", "pr", "update-branch", "123"], None, "github"),
    (["gh", "pr", "merge", "123"], None, "github"),
    (["gh", "pr", "checks", "123"], None, "github"),
    (["gh", "run", "rerun", "123", "--failed"], None, "github"),
    (["gh", "api", "repos/owner/repo/issues", "--method", "PATCH"], None, "github"),
    (["gh", "issue", "create"], None, "github"),
    (["gh", "issue", "comment", "123"], None, "github"),
    (["gh", "issue", "edit", "123"], None, "github"),
    (["gh", "issue", "close", "123"], None, "github"),
    (["gh", "release", "create", "v1.2.3"], None, "github"),
    (["gh", "workflow", "run", "validate.yml"], None, "github"),
    (["git", "commit", "-m", "demo"], None, "github"),
    (["git", "merge", "--ff-only", "--no-ff", "main"], None, "github"),
    (["git", "merge", "--ff-only", "--ff", "main"], None, "github"),
    (["git", "-Cpath", "-cuser.name=Human", "merge", "main"], None, "github"),
    (["git", "merge", "origin/main"], None, "github"),
    (["git", "-C", "path", "merge", "--continue"], None, "github"),
    (["git", "pull", "origin/main"], None, "github"),
    (["git", "-C", "path", "pull", "--continue"], None, "github"),
    (["git", "rebase", "origin/main"], None, "github"),
    (["git", "-C", "path", "rebase", "--continue"], None, "github"),
    (["git", "cherry-pick", "origin/main"], None, "github"),
    (["git", "-C", "path", "cherry-pick", "--continue"], None, "github"),
    (["git", "revert", "origin/main"], None, "github"),
    (["git", "-C", "path", "revert", "--continue"], None, "github"),
    (["git", "am", "origin/main"], None, "github"),
    (["git", "-C", "path", "am", "--continue"], None, "github"),
    (["git", "push", "origin", "branch"], None, "github"),
    (["git", "push", "--force", "origin", "branch"], None, "github"),
    (["git", "-c", "commit.gpgsign=false", "commit", "-m", "demo"], None, "github"),
    (["git", "-C", "path", "commit", "-m", "demo"], None, "github"),
    (["git", "-c", "http.extraHeader=x", "push", "origin", "branch"], None, "github"),
    (["git", "-C", "path", "push", "origin", "branch"], None, "github"),
    (["gh", "issue", "list", "--label", "plan"], None, "github-plan"),
    (["gh", "search", "issues", "repo:owner/repo"], None, "github-plan"),
    (["gh", "project", "item-list", "1"], None, "github-plan"),
    (["gh", "api", "graphql"], graphql_shell(), "github-plan"),
    (["curl", "inspection"], inspection_shell(), "jetbrains-inspection"),
    (["curl", "launchplane-apply"], launchplane_apply_shell(), "launchplane"),
    (["curl", "launchplane-merge-train"], launchplane_merge_train_shell(), "launchplane"),
    (["launchplane", "merge-train", "run-once"], None, "launchplane"),
)

NEGATIVE_EXPECTATIONS: tuple[tuple[list[str], str | None], ...] = (
    (["git", "reset", "--hard"], None),
    (["git", "-C", "path", "-c", "core.hooksPath=/dev/null", "merge", "--ff-only", "--no-autostash", "--no-overwrite-ignore", "abc"], None),
    (["git", "merge", "--ff-only", "main"], None),
    (["git", "merge", "--abort"], None),
    (["git", "rebase", "--abort"], None),
    (["git", "cherry-pick", "--quit"], None),
    (["git", "merge-base", "main", "topic"], None),
    (["git", "rebase-helper"], None),
    (["git-commit-as-bot", "--git-command", "merge", "main"], None),
    (["git", "-C", "path", "commit-graph", "write"], None),
    (["git", "-C", "path", "log", "--grep", "push"], None),
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
    for argv, shell, expected_skill in EXPECTATIONS:
        match = primary_match(argv, shell)
        if match is None:
            errors.append(f"{shlex.join(argv)}: expected {expected_skill}, got no match")
            continue
        if match.skill != expected_skill:
            errors.append(
                f"{shlex.join(argv)}: expected {expected_skill}, got {match.skill}/{match.policy_id}"
            )
    for argv, shell in NEGATIVE_EXPECTATIONS:
        match = primary_match(argv, shell)
        if match is not None:
            errors.append(f"{shlex.join(argv)}: expected no match, got {match.skill}/{match.policy_id}")
    errors.extend(precedence_self_test())
    return errors


def print_catalog(json_output: bool) -> None:
    catalog = policy_catalog()
    if json_output:
        print(json.dumps(catalog, indent=2, sort_keys=True))
        return
    for entry in catalog:
        matcher = entry["match"]
        if "argv_exact" in matcher:
            match_text = "argv_exact=" + shlex.join([str(token) for token in matcher["argv_exact"]])
        elif "argv_prefix" in matcher:
            match_text = "argv_prefix=" + shlex.join([str(token) for token in matcher["argv_prefix"]])
        else:
            match_text = "shell_regex=" + str(matcher.get("shell_regex", ""))
        print(f"{entry['skill']}\t{entry['id']}\t{entry['action']}\t{match_text}")


def self_test() -> None:
    mock_policies = []
    with patch(__name__ + ".iter_policies", side_effect=lambda: mock_policies):
        # Test case 1: Matcher precedence (argv_exact beats argv_prefix beats shell_regex)
        mock_policies = [
            (0, "skill-a", 0, {"id": "exact-policy", "match": {"argv_exact": ["demo"]}}),
            (1, "skill-b", 0, {"id": "prefix-policy", "match": {"argv_prefix": ["demo"]}}),
            (2, "skill-c", 0, {"id": "regex-policy", "match": {"shell_regex": "demo"}}),
        ]
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
        matches = simulate(["demo", "sub"])
        assert len(matches) == 2
        assert matches[0].skill == "skill-b"
        assert matches[0].policy_id == "long-prefix"

        # Test case 3: Skill order tie-breaking (alphabetical/list order)
        mock_policies = [
            (1, "skill-b", 0, {"id": "policy-b", "match": {"argv_prefix": ["demo"]}}),
            (0, "skill-a", 0, {"id": "policy-a", "match": {"argv_prefix": ["demo"]}}),
        ]
        matches = simulate(["demo"])
        assert len(matches) == 2
        assert matches[0].skill == "skill-a"

        # Test case 4: Policy index tie-breaking (index order)
        mock_policies = [
            (0, "skill-a", 1, {"id": "policy-second", "match": {"argv_prefix": ["demo"]}}),
            (0, "skill-a", 0, {"id": "policy-first", "match": {"argv_prefix": ["demo"]}}),
        ]
        matches = simulate(["demo"])
        assert len(matches) == 2
        assert matches[0].policy_id == "policy-first"

    print("ok validate-command-policy-simulator self-test")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="*", help="Command tokens to simulate")
    parser.add_argument("--shell", help="Shell string to use for shell_regex matching")
    parser.add_argument("--cwd", type=Path, help="Verified command working directory for repository exceptions")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--catalog", action="store_true", help="Print all structured command policies")
    parser.add_argument("--self-test", action="store_true", help="Run precedence self tests")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    if args.catalog:
        print_catalog(args.json)
        return 0

    if args.command:
        payload = [match.__dict__ for match in simulate(args.command, args.shell, cwd=args.cwd)]
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
