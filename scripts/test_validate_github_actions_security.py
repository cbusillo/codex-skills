#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML>=6.0.0",
# ]
# ///
"""Focused tests for validate_github_actions_security.py."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


SCRIPT = Path(__file__).with_name("validate_github_actions_security.py")


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("validate_github_actions_security", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load validate_github_actions_security.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def fixture_root(
    workflow: str,
    *,
    workflow_path: str = ".github/workflows/test.yml",
    action: str | None = None,
    action_path: str = ".github/actions/example/action.yaml",
) -> Iterator[Path]:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        workflow_file = root / workflow_path
        workflow_file.parent.mkdir(parents=True)
        workflow_file.write_text(workflow, encoding="utf-8")
        if action is not None:
            action_file = root / action_path
            action_file.parent.mkdir(parents=True)
            action_file.write_text(action, encoding="utf-8")
        yield root


def assert_contains(violations: list[str], expected: str) -> None:
    if not any(expected in violation for violation in violations):
        raise AssertionError(f"expected {expected!r} in {violations}")


def test_current_repository_passes() -> None:
    module = load_module()
    violations = module.validate_repository(module.ROOT)
    if violations:
        raise AssertionError(f"current repository must pass: {violations}")


def test_trust_classification_is_separate_from_pin_policy() -> None:
    module = load_module()
    if module.APPROVED_REMOTE_ACTIONS["actions/checkout"].trust != "GitHub-maintained":
        raise AssertionError("actions/checkout trust classification is missing")
    if (
        module.APPROVED_REMOTE_ACTIONS["astral-sh/setup-uv"].trust
        != "Approved third-party publisher"
    ):
        raise AssertionError("astral-sh/setup-uv trust classification is missing")
    if module.MUTABLE_REFERENCE_ALLOWLIST:
        raise AssertionError("mutable action reference allowlist must default to empty")


def test_accepts_pinned_remote_and_local_references() -> None:
    module = load_module()
    with fixture_root(
        """env:
  uses: this-is-an-environment-variable
jobs:
  reusable:
    uses: ./.github/workflows/local.yml
  test:
    runs-on: ubuntu-latest
    env:
      uses: this-is-also-an-environment-variable
    steps:
      - uses: ./local-action
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4.3.1
      - {uses: \"astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e\"} # v6.8.0
"""
    ) as root:
        violations = module.validate_repository(root, require_exact_sources=False)
        if violations:
            raise AssertionError(f"valid action references must pass: {violations}")


def test_rejects_mutable_short_unapproved_and_unprovenanced_references() -> None:
    module = load_module()
    with fixture_root(
        """jobs:
  test:
    steps:
      - uses: actions/checkout@v4 # v4.3.1
      - uses: astral-sh/setup-uv@d0cc045d # v6.8.0
      - uses: example/action@1111111111111111111111111111111111111111 # v1.0.0
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5
      - uses: ./local-action@main
      - uses: ./../outside
      - uses: docker://alpine:3.22
"""
    ) as root:
        violations = module.validate_repository(root, require_exact_sources=False)
        assert_contains(violations, "must use a 40-character SHA")
        assert_contains(violations, "unapproved remote action source 'example/action'")
        assert_contains(violations, "must document its release tag")
        assert_contains(violations, "local action reference must not include a revision")
        assert_contains(violations, "local action reference must remain inside the repository")
        assert_contains(violations, "remote action reference must include a full commit SHA")


def test_scans_referenced_composite_action_metadata() -> None:
    module = load_module()
    with fixture_root(
        """jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: ./.github/workflows/hidden-action
""",
        action="""runs:
  using: composite
  steps:
    - uses: actions/checkout@v4 # v4.3.1
""",
        action_path=".github/workflows/hidden-action/action.yml",
    ) as root:
        violations = module.validate_repository(root, require_exact_sources=False)
        assert_contains(violations, "must use a 40-character SHA")


def test_rejects_mutable_flow_style_reference() -> None:
    module = load_module()
    with fixture_root(
        """jobs:
  test:
    runs-on: ubuntu-latest
    steps: [{uses: actions/checkout@v4}]
"""
    ) as root:
        violations = module.validate_repository(root, require_exact_sources=False)
        assert_contains(violations, "must use a 40-character SHA")


def test_workflow_named_action_yml_is_not_misclassified() -> None:
    module = load_module()
    with fixture_root(
        """jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4 # v4.3.1
""",
        workflow_path=".github/workflows/action.yml",
    ) as root:
        violations = module.validate_repository(root, require_exact_sources=False)
        assert_contains(violations, "must use a 40-character SHA")


def test_direct_uses_overrides_merged_anchor_value() -> None:
    module = load_module()
    with fixture_root(
        """action-defaults: &action-defaults
  uses: actions/checkout@v4 # v4.3.1
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - <<: *action-defaults
        uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4.3.1
"""
    ) as root:
        violations = module.validate_repository(root, require_exact_sources=False)
        if violations:
            raise AssertionError(f"direct mapping value must override merged value: {violations}")


def test_rejects_stale_mutable_reference_allowlist_entries() -> None:
    module = load_module()
    original_allowlist = module.MUTABLE_REFERENCE_ALLOWLIST
    module.MUTABLE_REFERENCE_ALLOWLIST = {
        Path(".github/workflows/missing.yml"): frozenset({"actions/checkout@v4"})
    }
    try:
        with fixture_root("jobs: {}\n") as root:
            violations = module.validate_repository(root, require_exact_sources=False)
            assert_contains(violations, "mutable reference allowlist entries are unused")
    finally:
        module.MUTABLE_REFERENCE_ALLOWLIST = original_allowlist


def main() -> int:
    test_current_repository_passes()
    test_trust_classification_is_separate_from_pin_policy()
    test_accepts_pinned_remote_and_local_references()
    test_rejects_mutable_short_unapproved_and_unprovenanced_references()
    test_scans_referenced_composite_action_metadata()
    test_rejects_mutable_flow_style_reference()
    test_workflow_named_action_yml_is_not_misclassified()
    test_direct_uses_overrides_merged_anchor_value()
    test_rejects_stale_mutable_reference_allowlist_entries()
    print("ok test-github-actions-security")
    return 0


if __name__ == "__main__":
    sys.exit(main())
