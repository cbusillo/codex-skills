#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Run the Claude Code command-policy hook against the catalog's real policies."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "command_policy_hook.py"
sys.path.insert(0, str(HOOK.parent))

import command_policy_hook  # noqa: E402

SIMULATOR = command_policy_hook.load_simulator()


def run_hook(payload: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK)], input=payload, capture_output=True, text=True, check=False
    )


def bash(command: str) -> subprocess.CompletedProcess[str]:
    return run_hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}))


class CommandPolicyHookTests(unittest.TestCase):
    def test_every_argv_policy_blocks_its_command_with_its_own_message(self) -> None:
        catalog = {(entry["skill"], entry["id"]): entry for entry in SIMULATOR.policy_catalog()}
        argv_policies = [
            entry for entry in catalog.values() if {"argv_prefix", "argv_exact"} & set(entry["match"])
        ]
        self.assertTrue(argv_policies)
        for entry in argv_policies:
            argv = [str(token) for token in entry["match"].get("argv_exact") or entry["match"]["argv_prefix"]]
            # Another policy may legitimately take precedence; the simulator decides which.
            winner = SIMULATOR.primary_match(argv)
            expected = catalog[(winner.skill, winner.policy_id)]
            with self.subTest(policy=entry["id"]):
                result = bash(shlex.join(argv))
                self.assertEqual(result.returncode, 2)
                self.assertIn(expected["id"], result.stderr)
                if expected.get("message"):
                    self.assertIn(str(expected["message"]), result.stderr)

    def test_a_policy_command_is_found_inside_a_compound_line(self) -> None:
        entry = next(e for e in SIMULATOR.policy_catalog() if "argv_prefix" in e["match"])
        command = shlex.join(str(token) for token in entry["match"]["argv_prefix"])
        for line in (
            f"cd /tmp && {command} 5",
            f"PAGER=cat OTHER=1 {command} 5",
            f"true; ( {command} 5 ) | cat",
        ):
            with self.subTest(line=line):
                self.assertEqual(bash(line).returncode, 2)

    def test_commands_no_policy_names_run(self) -> None:
        entry = next(e for e in SIMULATOR.policy_catalog() if "argv_prefix" in e["match"])
        command = shlex.join(str(token) for token in entry["match"]["argv_prefix"])
        for line in ("git status", f"printf %s {shlex.quote(command)}"):
            with self.subTest(line=line):
                self.assertIsNone(SIMULATOR.primary_match(shlex.split(line), line), "fixture must be policy-free")
                result = bash(line)
                self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_a_preferred_replacement_is_itself_allowed(self) -> None:
        for entry in SIMULATOR.policy_catalog():
            for preferred in entry["preferred"]:
                if preferred.get("example_argv"):
                    line = shlex.join(str(token) for token in preferred["example_argv"])
                    with self.subTest(policy=entry["id"], line=line):
                        self.assertEqual(bash(line).returncode, 0, bash(line).stderr)

    def test_other_tools_and_unreadable_events_are_let_through(self) -> None:
        self.assertEqual(run_hook(json.dumps({"tool_name": "Read", "tool_input": {}})).returncode, 0)
        for payload in ("not json", json.dumps({"tool_name": "Bash"})):
            with self.subTest(payload=payload):
                result = run_hook(payload)
                self.assertEqual(result.returncode, 0)
                self.assertIn("skipped", result.stderr)


if __name__ == "__main__":
    unittest.main()
