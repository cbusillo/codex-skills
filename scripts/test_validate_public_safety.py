#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused tests for validate-public-safety.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).with_name("validate-public-safety.py")


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("validate_public_safety", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load validate-public-safety.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def scan_line(module: Any, line: str) -> list[Any]:
    return module.scan_line(module.ROOT / "fixture.txt", 7, line)


def test_detects_secret_shapes() -> None:
    module = load_module()
    token = "ghp_" + "123456789012345678901234"
    findings = scan_line(module, f"token={token}")
    if len(findings) != 1 or findings[0].rule != "github-classic-token":
        raise AssertionError(f"expected github token finding: {findings}")
    if findings[0].line_number != 7:
        raise AssertionError(f"expected source line number to be preserved: {findings}")


def test_detects_credentialed_urls() -> None:
    module = load_module()
    credentialed_url = "https://" + "user:pass" + "@example.invalid/path"
    credentialed = scan_line(module, f"url={credentialed_url}")
    rules = {finding.rule for finding in credentialed}
    expected = {"credentialed-url"}
    if rules != expected:
        raise AssertionError(f"expected {expected}, got {rules}")


def test_allows_documented_placeholders() -> None:
    module = load_module()
    placeholders = [
        "GH_TOKEN=github_pat_xxx",
        "GH_TOKEN=ghp_example",
        "OPENAI_API_KEY=sk-[A-Za-z0-9...]",
        "AWS_ACCESS_KEY_ID=AKIA[0-9A-Z]",
    ]
    for placeholder in placeholders:
        findings = scan_line(module, placeholder)
        if findings:
            raise AssertionError(f"documented placeholder should pass: {placeholder}: {findings}")


def main() -> int:
    test_detects_secret_shapes()
    test_detects_credentialed_urls()
    test_allows_documented_placeholders()
    print("ok test-validate-public-safety")
    return 0


if __name__ == "__main__":
    sys.exit(main())
