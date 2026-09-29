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
                {"type": "user", "message": {"content": [{"type": "text", "text": f"Base directory for this skill: {runner.ROOT / 'skills/babysit-pr'}\n"}]}},
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
            read = f"cat {runner.ROOT}/skills/github/SKILL.md"
            (root / "shell-events.jsonl").write_text("\n".join(map(json.dumps, [
                {"command": read, "allowed": True},
                {"command": "uv run /catalog/skills/github/scripts/gh-pr.py merge 17", "allowed": False},
            ])))
            trace = root / "trace.jsonl"
            for exit_code, passed in [(1, False), (0, True)]:
                trace.write_text(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": read, "exit_code": exit_code}}))
                self.assertEqual(runner.score_run("codex", "direction-merge", root)["passed"], passed)

    def test_a_foreign_catalog_cannot_supply_the_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = "cat /installed/skills/github/SKILL.md"
            (root / "shell-events.jsonl").write_text("\n".join(map(json.dumps, [
                {"command": command, "allowed": True},
                {"command": "uv run /catalog/skills/github/scripts/gh-pr.py merge 17", "allowed": False},
            ])))
            (root / "trace.jsonl").write_text(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": command, "exit_code": 0}}))
            score = runner.score_run("codex", "direction-merge", root)
            self.assertFalse(score["checks"]["tested_catalog_only"])
            self.assertFalse(score["checks"]["owner_before_first_operation"])

    def test_merge_arguments_are_checked_by_the_real_parser(self) -> None:
        self.assertTrue(runner.valid_merge_arguments(runner.ROOT, "uv run gh-pr.py --repo owner/repo merge 17 --method merge"))
        self.assertFalse(runner.valid_merge_arguments(runner.ROOT, "uv run gh-pr.py merge 17 --repo owner/repo --method merge"))

    def test_a_load_in_an_earlier_turn_does_not_cover_a_later_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {"type": "user", "message": {"content": [{"type": "text", "text": f"Base directory for this skill: {runner.ROOT / 'skills/github'}\n"}]}}
            launched = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "Launching skill: shared:github"}]}}
            merge = call("Bash", {"command": "uv run gh-pr.py --repo owner/repo merge 18 --method merge"})
            turns = [{"expect": {"owner": "github", "helper": "gh-pr.py"}}] * 2
            first = [{"type": "turn_marker", "turn": 1}, call("Skill", {"skill": "shared:github"}), launched, base, merge]
            for second, passed in [([merge], False), ([call("Skill", {"skill": "shared:github"}), launched, merge], True)]:
                (root / "trace.jsonl").write_text("\n".join(map(json.dumps, [*first, {"type": "turn_marker", "turn": 2}, *second])))
                score = runner.score_turns("claude", turns, root)
                self.assertTrue(score["checks"]["turn1_owner_before_first_operation"])
                self.assertEqual(score["checks"]["turn2_owner_before_first_operation"], passed)

    def test_a_repeat_invocation_needs_an_earlier_load_from_the_tested_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launched = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "Launching skill: shared:github"}]}}
            merge = call("Bash", {"command": "uv run gh-pr.py merge 18"})
            (root / "trace.jsonl").write_text("\n".join(map(json.dumps, [{"type": "turn_marker", "turn": 1}, launched, merge])))
            score = runner.score_turns("claude", [{"expect": {"owner": "github", "helper": "gh-pr.py"}}], root)
            self.assertFalse(score["checks"]["turn1_owner_before_first_operation"])

    def test_an_owner_without_one_helper_must_precede_the_first_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {"type": "user", "message": {"content": [{"type": "text", "text": f"Base directory for this skill: {runner.ROOT / 'skills/work-closeout'}\n"}]}}
            read = call("Bash", {"command": "git status"})
            remove = call("Bash", {"command": "git worktree remove ../task"})
            turns = [{"expect": {"owner": "work-closeout"}}]
            for messages, passed in [([read, remove, base], False), ([read, base, remove], True), ([read], False)]:
                (root / "trace.jsonl").write_text("\n".join(map(json.dumps, [{"type": "turn_marker", "turn": 1}, *messages])))
                self.assertEqual(runner.score_turns("claude", turns, root)["passed"], passed)

    def test_decision_grades_judge_the_operations_and_final_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {"type": "user", "message": {"content": [{"type": "text", "text": f"Base directory for this skill: {runner.ROOT / 'skills/babysit-pr'}\n"}]}}
            expect = {"owner": ["github", "babysit-pr"], "operation": "--watch", "require": "--pr 21", "forbid": "--once", "final": "watching"}
            turns = [{"expect": expect}]
            for command, final, passed in [("uv run gh_pr_watch.py --pr 21 --watch", "Still watching.", True),
                                           ("uv run gh_pr_watch.py --pr 21 --once", "Still watching.", False),
                                           ("uv run gh_pr_watch.py --pr 21 --watch", "Done.", False)]:
                messages = [{"type": "turn_marker", "turn": 1}, base, call("Bash", {"command": command}),
                            {"type": "result", "result": final}]
                (root / "trace.jsonl").write_text("\n".join(map(json.dumps, messages)))
                self.assertEqual(runner.score_turns("claude", turns, root)["passed"], passed)

    def test_a_required_reference_is_read_before_the_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {"type": "user", "message": {"content": [{"type": "text", "text": f"Base directory for this skill: {runner.ROOT / 'skills/github'}\n"}]}}
            turns = [{"expect": {"owner": "github", "read": "cli-reference\\.md"}}]
            view = call("Bash", {"command": "uv run scripts/gh-pr.py view 23"})
            by_tool = call("Read", {"file_path": "/catalog/skills/github/references/cli-reference.md"})
            by_shell = call("Bash", {"command": "sed -n 170,230p references/cli-reference.md"})
            for messages, passed in [([base, by_tool, view], True), ([base, by_shell, view], True),
                                     ([base, view, by_tool], False), ([base, view], False)]:
                (root / "trace.jsonl").write_text("\n".join(map(json.dumps, [{"type": "turn_marker", "turn": 1}, *messages])))
                self.assertEqual(runner.score_turns("claude", turns, root)["passed"], passed)

    def test_a_pipe_is_a_read_only_when_every_stage_is(self) -> None:
        self.assertTrue(runner.read_only("rg --files -g 'SKILL.md' | sed -n '1,80p'"))
        self.assertFalse(runner.read_only("cat script | sh"))
        self.assertFalse(runner.read_only("ls | xargs rm"))

    def test_duplicate_startup_context_fails_the_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hook = {"type": "system", "subtype": "hook_response", "output": "# Using Skills\n"}
            (root / "trace.jsonl").write_text("\n".join(map(json.dumps, [hook, hook])))
            self.assertFalse(runner.score_run("claude", "merge-discussion-only", root)["passed"])


if __name__ == "__main__":
    unittest.main()
