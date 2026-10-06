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


def test_frontmatter_carry_stops_at_boundaries() -> None:
    module = load_module()
    for qualifier, expected in (("repository", [(4, "owner")]), ("policy", [])):
        word = "owner" if qualifier == "repository" else "administrator"
        text = f"---\ndescription: Sync the {qualifier}\n---\n{word} approves."
        if module.findings(text) != expected:
            raise AssertionError(module.findings(text))
        # A separate prose field ends carry, with or without skipped metadata.
        for boundary in ("purpose: >-", "argv: [sync]\npurpose: >-"):
            text = (
                f"---\ndescription: Sync the {qualifier}\n{boundary}\n"
                f"  {word} approves.\n---\n"
            )
            expected_line = 4 if boundary.startswith("purpose:") else 5
            expected = [(expected_line, "owner")] if word == "owner" else []
            if module.findings(text) != expected:
                raise AssertionError(module.findings(text))


def test_frontmatter_paragraph_breaks_preserve_prose_fields() -> None:
    module = load_module()
    for field in ("description", "purpose", "message"):
        for blank in ("", "  "):
            # Both top-level and nested policy messages resume after a break.
            for prefix, indent in (("", ""), ("policies:\n  - id: x\n", "    ")):
                text = (
                    f"---\n{prefix}{indent}{field}: >\n{indent}  Sync files.\n"
                    f"{blank}\n{indent}  Ask the owner first.\n"
                    f"{indent}argv: >\n{indent}  operator owner\n---\n"
                )
                expected_line = 5 if not prefix else 7
                if module.findings(text) != [(expected_line, "owner")]:
                    raise AssertionError(module.findings(text))
        # A blank paragraph resets phrase carry while leaving the field active.
        for qualifier, word, expected in (
            ("repository", "owner", [(5, "owner")]),
            ("policy", "administrator", []),
        ):
            text = f"---\n{field}: >\n  Sync the {qualifier}\n\n  {word} approves.\n---\n"
            if module.findings(text) != expected:
                raise AssertionError(module.findings(text))


def test_preserves_carry_within_folded_frontmatter_fields() -> None:
    module = load_module()
    for field in ("description", "purpose", "message"):
        for separator in ("", "-"):
            text = f"---\n{field}: >-\n  Ask the policy{separator}\n  administrator first.\n---\n"
            expected_word = "policy-administrator" if separator else "policy administrator"
            if module.findings(text) != [(4, expected_word)]:
                raise AssertionError(module.findings(text))
            text = f"---\n{field}: >-\n  Ask the repository{separator}\n  owner first.\n---\n"
            if module.findings(text):
                raise AssertionError(module.findings(text))


def test_flags_blockquote_and_hyphen_wraps() -> None:
    module = load_module()
    for text, expected_word in (
        ("> The policy\n> administrator approves.", "policy administrator"),
        ("The policy-\nadministrator approves.", "policy-administrator"),
        ("> > The POLICY-  \n> > administrators approve.", "POLICY-administrators"),
        ("> The signed-in policy\nadministrator approves access.", "policy administrator"),
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
    text = "> The policy\n> ```\n> code\n> ```\n> administrator approves."
    if module.findings(text):
        raise AssertionError(module.findings(text))
    text = "> The repository\n> ```\n> code\n> ```\n> owner approves."
    if module.findings(text) != [(5, "owner")]:
        raise AssertionError(module.findings(text))


def test_allows_wrapped_github_sense() -> None:
    module = load_module()
    for text in (
        "> The repository\n> owner applies the ruleset.",
        "The repository-\nowner applies the ruleset.",
        "> The repository-\n> owner applies the ruleset.",
        "> Ask the repository\nowner to apply the ruleset.",
    ):
        if module.findings(text):
            raise AssertionError(module.findings(text))


def test_quoted_fence_literals_stay_inside_unquoted_code() -> None:
    module = load_module()
    for fence in ("```", "~~~"):
        text = f"{fence}text\n> {fence}bash\noperator = owner\n{fence}\nAsk the owner."
        if module.findings(text) != [(5, "owner")]:
            raise AssertionError(module.findings(text))


def test_quote_boundaries_do_not_hide_role_prose() -> None:
    module = load_module()
    for text, expected in (
        ("> ```bash\n> make\n\nAsk the owner.", [(4, "owner")]),
        ("Settings live in the repository\n> Owner approval is required.", [(2, "Owner")]),
        ("> Settings live in the repository\n>> Owner approval is required.", [(2, "Owner")]),
    ):
        if module.findings(text) != expected:
            raise AssertionError(module.findings(text))


def test_only_matching_bare_fences_close_code() -> None:
    module = load_module()
    for text, expected_line in (
        ("> ```markdown\n> > ```bash\n> > run as operator\n> > ```\n> ```\nOwner approves.", 6),
        ("```markdown\n```bash\nrun as operator\n```\nOwner approves.", 5),
    ):
        if module.findings(text) != [(expected_line, "Owner")]:
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
    test_frontmatter_carry_stops_at_boundaries()
    test_frontmatter_paragraph_breaks_preserve_prose_fields()
    test_preserves_carry_within_folded_frontmatter_fields()
    test_flags_blockquote_and_hyphen_wraps()
    test_never_carries_prose_across_fences()
    test_allows_wrapped_github_sense()
    test_quoted_fence_literals_stay_inside_unquoted_code()
    test_quote_boundaries_do_not_hide_role_prose()
    test_only_matching_bare_fences_close_code()
    test_allows_github_sense_and_code()
    print("ok test-validate-role-words")
    return 0


if __name__ == "__main__":
    sys.exit(main())
