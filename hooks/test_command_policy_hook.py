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
        [sys.executable, str(HOOK)], input=payload, capture_output=True, text=True
    )


def bash(command: str) -> subprocess.CompletedProcess[str]:
    return run_hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}))


class CommandPolicyHookTests(unittest.TestCase):
    def test_auth_wrapper_keeps_gh_policy_ownership(self) -> None:
        for line in (
            "gh-with-env-token pr merge 17 --merge",
            "skills/github/scripts/gh-with-env-token --print-auth-account pr merge 17 --merge",
            "command /catalog/github/scripts/gh-with-env-token --require-automation-auth pr merge 17",
            "env -u GH_TOKEN /catalog/github/scripts/gh-with-env-token pr merge 17",
            "/usr/bin/env -C /repo FOO=1 gh-with-env-token pr merge 17",
            "uv run --python 3.12 gh-with-env-token pr merge 17",
            "bash -lc 'cd /repo && gh-with-env-token pr merge 17'",
        ):
            with self.subTest(line=line):
                result = bash(line)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("Load the `github` skill", result.stderr)
                self.assertIn("gh-pr.py", result.stderr)

    def test_wrapper_reads_auth_checks_and_preferred_helpers_remain_allowed(self) -> None:
        for line in (
            "gh-with-env-token api repos/owner/repo",
            "gh-with-env-token --check pr merge 17",
            "gh-with-env-token --print-auth-account --check pr merge 17",
            "uv run skills/github/scripts/gh-pr.py merge 17 --method merge",
            "skills/github/scripts/git-push-as-bot origin work/task",
            "printf '%s' 'gh-with-env-token pr merge 17'",
        ):
            with self.subTest(line=line):
                self.assertEqual(bash(line).returncode, 0, bash(line).stderr)

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

    def test_ruleset_policy_blocks_writes_but_allows_reads(self) -> None:
        for line in (
            "gh ruleset list",
            "gh ruleset view 123",
            "gh-with-env-token api repos/owner/repo/rulesets --method GET",
        ):
            with self.subTest(line=line):
                self.assertEqual((bash(line).returncode, bash(line).stderr), (0, ""))
        for line in (
            "gh api -X POST repos/owner/repo/rulesets --input payload.json",
            "gh-with-env-token api repos/owner/repo/rulesets/123 --method PUT --input payload.json",
            "gh-with-env-token api repos/owner/repo/rulesets --input payload.json",
            "gh-with-env-token api repos/owner/repo/rulesets -f name=x -f target=branch",
            "gh-with-env-token api --method=PUT repos/owner/repo/rulesets/123 --input payload.json",
            "gh-with-env-token api -XDELETE repos/owner/repo/rulesets/123",
        ):
            with self.subTest(line=line):
                result = bash(line)
                self.assertEqual(result.returncode, 2)
                if line.startswith("gh-with-env-token"):
                    self.assertIn("prefer-standard-ruleset-helper", result.stderr)

    def test_git_global_options_do_not_hide_commit_or_push(self) -> None:
        for line, policy in (
            ("git -c commit.gpgsign=false commit -m demo", "prefer-bot-commit-helper-with-git-options"),
            ("cd /tmp && git -C 'a path' commit -m demo", "prefer-bot-commit-helper-with-git-options"),
            ("bash -lc 'git --no-pager -C repo commit'", "prefer-bot-commit-helper-with-git-options"),
            ("git -c user.name=\"Shiny Code\" commit -m demo", "prefer-bot-commit-helper-with-git-options"),
            ("git -C repo push origin branch", "prefer-bot-push-helper-with-git-options"),
        ):
            with self.subTest(line=line):
                result = bash(line)
                self.assertEqual(result.returncode, 2)
                self.assertIn(policy, result.stderr)
        for line in (
            "git -C repo status",
            "git -C repo commit-graph write",
            "git -C repo log --grep commit",
            "rg -n 'git -C repo commit' docs/",
        ):
            with self.subTest(line=line):
                self.assertEqual((bash(line).returncode, bash(line).stderr), (0, ""))

    def test_a_preferred_replacement_is_itself_allowed(self) -> None:
        for entry in SIMULATOR.policy_catalog():
            for preferred in entry["preferred"]:
                if preferred.get("example_argv"):
                    line = shlex.join(str(token) for token in preferred["example_argv"])
                    with self.subTest(policy=entry["id"], line=line):
                        self.assertEqual(bash(line).returncode, 0, bash(line).stderr)

    def test_the_replacement_shown_names_a_script_that_exists_here(self) -> None:
        for entry in SIMULATOR.policy_catalog():
            for preferred in entry["preferred"]:
                for token in map(str, preferred.get("example_argv") or []):
                    if not token.endswith((".py", ".sh")) or token.startswith("/"):
                        continue
                    with self.subTest(policy=entry["id"], token=token):
                        shown = command_policy_hook.runnable(token, entry["skill"])
                        self.assertTrue(Path(shown).is_absolute(), shown)
                        self.assertTrue(Path(shown).is_file(), shown)

    def test_the_block_message_shows_the_resolved_replacement_and_it_is_allowed(self) -> None:
        result = bash("gh issue list")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("$CODE_HOME", result.stderr)
        shown = next(line for line in result.stderr.splitlines() if line.startswith("Run instead: "))
        self.assertTrue(Path(shlex.split(shown.removeprefix("Run instead: "))[2]).is_file(), shown)
        self.assertEqual(bash(shown.removeprefix("Run instead: ")).returncode, 0)

    def test_placeholders_and_flags_are_shown_as_written(self) -> None:
        for token in ("<query>", "--body-file", "uv", "/path/to/repo", "$HOME/x/y.py", "missing/script.py"):
            with self.subTest(token=token):
                self.assertEqual(command_policy_hook.runnable(token, "github-plan"), token)

    def test_a_policy_command_is_found_behind_what_agents_put_in_front_of_it(self) -> None:
        entry = next(e for e in SIMULATOR.policy_catalog() if "argv_prefix" in e["match"])
        prefix = [str(token) for token in entry["match"]["argv_prefix"]]
        command = shlex.join(prefix)
        tool, rest = prefix[0], shlex.join(prefix[1:])
        for line in (
            f"command {command} 5",
            f"exec {command} 5",
            f"time {command} 5",
            f"uv run --quiet {command} 5",
            f"env -i FOO=1 {command} 5",
            f"FOO=1 command /opt/homebrew/bin/{tool} {rest} 5",
            f"bash -lc {shlex.quote(f'cd /tmp && {command} 5')}",
        ):
            with self.subTest(line=line):
                self.assertEqual(bash(line).returncode, 2)

    def test_unwrapping_does_not_block_what_no_policy_names(self) -> None:
        entry = next(e for e in SIMULATOR.policy_catalog() if "argv_prefix" in e["match"])
        command = shlex.join(str(token) for token in entry["match"]["argv_prefix"])
        # The last two are wrappers the hook deliberately does not unwrap; see its docstring.
        for line in ("env", "uv run", "bash -c 'git status'", f"echo {command}", f"xargs {command}", f"sudo {command}"):
            with self.subTest(line=line):
                self.assertEqual((bash(line).returncode, bash(line).stderr), (0, ""))

    def test_other_tools_and_unreadable_events_are_let_through(self) -> None:
        self.assertEqual(run_hook(json.dumps({"tool_name": "Read", "tool_input": {}})).returncode, 0)
        for payload in ("not json", json.dumps({"tool_name": "Bash"})):
            with self.subTest(payload=payload):
                result = run_hook(payload)
                self.assertEqual(result.returncode, 0)
                self.assertIn("skipped", result.stderr)


if __name__ == "__main__":
    unittest.main()
