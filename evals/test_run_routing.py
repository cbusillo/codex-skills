#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Routing grades reject missing owners, policy redirects, and duplicate context."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("run_routing", Path(__file__).with_name("run-routing.py"))
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def call(name: str, arguments: dict[str, str]) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": arguments}]}}


class RoutingScoreTests(unittest.TestCase):
    def test_claude_redirect_is_not_first_try_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            messages = [
                call("Bash", {"command": "gh pr checks 17"}),
                call("Skill", {"skill": "shared:babysit-pr"}),
                call("Bash", {"command": "uv run gh_pr_watch.py --pr 17 --watch"}),
            ]
            trace = root / "trace.jsonl"
            trace.write_text("\n".join(map(json.dumps, messages)))
            score = runner.score_run("claude", "github-ci-watch", root)
            self.assertFalse(score["checks"]["owner_before_first_operation"])
            self.assertFalse(score["checks"]["helper_first"])
            trace.write_text("\n".join(map(json.dumps, messages[1:])))
            self.assertTrue(runner.score_run("claude", "github-ci-watch", root)["passed"])

    def test_codex_requires_a_successful_read_before_the_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            read = "cat /catalog/skills/github/SKILL.md"
            (root / "shell-events.jsonl").write_text("\n".join(map(json.dumps, [
                {"command": read, "allowed": True},
                {"command": "uv run /catalog/skills/github/scripts/gh-pr.py merge 17", "allowed": False},
            ])))
            trace = root / "trace.jsonl"
            for exit_code, passed in [(1, False), (0, True)]:
                trace.write_text(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": read, "exit_code": exit_code}}))
                self.assertEqual(runner.score_run("codex", "direction-merge", root)["passed"], passed)

    def test_duplicate_startup_context_fails_the_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hook = {"type": "system", "subtype": "hook_response", "output": "# Using Skills\n"}
            (root / "trace.jsonl").write_text("\n".join(map(json.dumps, [hook, hook])))
            self.assertFalse(runner.score_run("claude", "merge-discussion-only", root)["passed"])


if __name__ == "__main__":
    unittest.main()
