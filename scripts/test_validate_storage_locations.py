#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused tests for validate_storage_locations.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).with_name("validate_storage_locations.py")


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("validate_storage_locations", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load validate_storage_locations.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_flags_undefined_location_in_prose() -> None:
    module = load_module()
    for text in (
        "Set it to the task-private directory on that volume.",
        "Keep it in task-private evidence.",
        "---\ndescription: Save output in Task Private storage.\n---\n",
        "Use the task\nprivate directory.",
        "Use the task-\nprivate directory.",
        "> Use the task\n> private directory.",
        "---\ndescription: >-\n  Use the task\n  private directory.\n---\n",
    ):
        if not module.findings(text):
            raise AssertionError(f"expected a finding: {text!r}")


def test_ignores_code_and_named_locations() -> None:
    module = load_module()
    for text in (
        "The old `task-private` folder was removed.",
        "```sh\nls /Volumes/X/task-private\n```\n",
        "Keep it in the session scratchpad or the host's task-evidence location.",
        "Finish the task\n\nprivate notes stay local.",
        "Finish the task\n```\nprivate\n```\n",
    ):
        if module.findings(text):
            raise AssertionError(module.findings(text))


def main() -> int:
    test_flags_undefined_location_in_prose()
    test_ignores_code_and_named_locations()
    print("ok test-validate-storage-locations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
