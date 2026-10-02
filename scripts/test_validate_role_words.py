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
    ):
        if not module.findings(line):
            raise AssertionError(f"expected a finding: {line}")


def test_flags_frontmatter_description_only() -> None:
    module = load_module()
    text = "---\nname: x\ndescription: Use when the owner asks.\npolicy: owner\n---\nBody.\n"
    if [number for number, _ in module.findings(text)] != [3]:
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
        )
    )
    if module.findings(text):
        raise AssertionError(module.findings(text))


def main() -> int:
    test_flags_role_words_in_prose()
    test_flags_frontmatter_description_only()
    test_allows_github_sense_and_code()
    print("ok test-validate-role-words")
    return 0


if __name__ == "__main__":
    sys.exit(main())
