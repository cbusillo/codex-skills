#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused tests for validate_role_words.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).with_name("validate_role_words.py")


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("validate_role_words", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load validate_role_words.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_flags_role_words_in_prose() -> None:
    module = load_module()
    for line in (
        "Ask the owner before merging.",
        "The Site Owner approves the release.",
        "An operator grants access.",
        "Record the owner's decision.",
        "The policy administrator approves access.",
    ):
        if not module.findings(line):
            raise AssertionError(f"expected a finding: {line}")


def test_flags_frontmatter_description_only() -> None:
    module = load_module()
    text = "---\nname: x\ndescription: Use when the owner asks.\npolicy: owner\n---\nBody.\n"
    if [number for number, _ in module.findings(text)] != [3]:
        raise AssertionError(module.findings(text))


def test_flags_wrapped_policy_administrator() -> None:
    module = load_module()
    for text in (
        "The signed-in policy\nadministrator approves access.",
        "The signed-in POLICY  \n  administrators approve access.",
        "---\ndescription: >-\n  Ask the policy\n  administrator first.\n---\n",
    ):
        expected_line = 4 if text.startswith("---") else 2
        found = module.findings(text)
        if len(found) != 1 or found[0][0] != expected_line:
            raise AssertionError(found)


def test_flags_folded_frontmatter_prose() -> None:
    module = load_module()
    text = (
        "---\ndescription: >-\n  Use when the owner asks.\n"
        "policies:\n  - id: x\n    message: Ask\n      the operator first.\n"
        "    argv: [owner]\n---\n"
    )
    if [number for number, _ in module.findings(text)] != [3, 7]:
        raise AssertionError(module.findings(text))


def test_flags_blockquote_and_hyphen_wraps() -> None:
    module = load_module()
    for text, expected_word in (
        ("> The policy\n> administrator approves.", "policy administrator"),
        ("The policy-\nadministrator approves.", "policy-administrator"),
        ("> > The POLICY-  \n> > administrators approve.", "POLICY-administrators"),
    ):
        found = module.findings(text)
        if found != [(2, expected_word)]:
            raise AssertionError(found)


def test_never_carries_prose_across_fences() -> None:
    module = load_module()
    for fence in ("```", "~~~", "> ```"):
        text = f"The policy\n{fence}\ncode\n{fence}\nadministrator approves."
        if module.findings(text):
            raise AssertionError(module.findings(text))
        # A repository qualifier before code must not exempt unrelated prose.
        text = f"The repository\n{fence}\ncode\n{fence}\nowner approves."
        if module.findings(text) != [(5, "owner")]:
            raise AssertionError(module.findings(text))


def test_allows_wrapped_github_sense() -> None:
    module = load_module()
    for text in (
        "> The repository\n> owner applies the ruleset.",
        "The repository-\nowner applies the ruleset.",
        "> The repository-\n> owner applies the ruleset.",
    ):
        if module.findings(text):
            raise AssertionError(module.findings(text))


def test_quoted_fence_literals_stay_inside_unquoted_code() -> None:
    module = load_module()
    for fence in ("```", "~~~"):
        text = f"{fence}text\n> {fence}bash\noperator = owner\n{fence}\nAsk the owner."
        if module.findings(text) != [(5, "owner")]:
            raise AssertionError(module.findings(text))


def test_allows_github_sense_and_code() -> None:
    module = load_module()
    text = "\n".join(
        (
            "Pass `OWNER/REPO` and read `owner_review` fields.",
            "Only the repository owner can apply the ruleset; code owners review it.",
            "Search `user:<owner>` in [the docs](https://example.test/owner).",
            "<!-- owner note -->",
            "```",
            "operator = owner",
            "```",
            "````markdown",
            "```",
            "owner = operator",
            "```",
            "````",
            "Ask the repository",
            "owner to apply the ruleset.",
        )
    )
    if module.findings(text):
        raise AssertionError(module.findings(text))


def main() -> int:
    test_flags_role_words_in_prose()
    test_flags_frontmatter_description_only()
    test_flags_wrapped_policy_administrator()
    test_flags_folded_frontmatter_prose()
    test_flags_blockquote_and_hyphen_wraps()
    test_never_carries_prose_across_fences()
    test_allows_wrapped_github_sense()
    test_quoted_fence_literals_stay_inside_unquoted_code()
    test_allows_github_sense_and_code()
    print("ok test-validate-role-words")
    return 0


if __name__ == "__main__":
    sys.exit(main())
