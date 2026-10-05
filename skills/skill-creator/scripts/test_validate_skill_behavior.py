#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Behavior probes accept valid command metadata independently of YAML layout."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("skill_behavior", Path(__file__).with_name("validate-skill-behavior.py"))
assert SPEC and SPEC.loader
behavior = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(behavior)


class CommandMetadataTests(unittest.TestCase):
    def command_argv(self, declaration: str) -> list[str]:
        validator = behavior.validator_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "fixture-skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: fixture-skill\ncommands:\n"
                "  - name: fixture-command\n" + declaration + "\n---\n"
            )
            with mock.patch.object(behavior, "ROOT", root), mock.patch.object(behavior, "validator_module", return_value=validator):
                return behavior.command_argv("fixture-skill", "fixture-command")

    def test_supported_yaml_layouts_deliver_the_same_command(self) -> None:
        for declaration in (
            '    example_argv: ["demo", "--flag"]',
            '    example_argv:\n      - demo\n      - --flag',
            '    example_argv: [\n      "demo",\n      "--flag"\n    ]',
        ):
            with self.subTest(declaration=declaration):
                self.assertEqual(self.command_argv(declaration), ["demo", "--flag"])

    def test_invalid_argument_lists_fail_before_execution(self) -> None:
        for declaration in ("    example_argv: []", "    example_argv: demo", "    example_argv: [demo, 7]", "    source: skill"):
            with self.subTest(declaration=declaration), self.assertRaises(AssertionError):
                self.command_argv(declaration)


if __name__ == "__main__":
    unittest.main()
