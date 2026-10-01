#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Run the Claude Code command-policy hook against the catalog's real policies."""

from __future__ import annotations

import contextlib
import functools
import io
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "command_policy_hook.py"
sys.path.insert(0, str(HOOK.parent))

import command_policy_hook  # noqa: E402

VALIDATOR_PATH = HOOK.parents[1] / "skills/skill-creator/scripts/quick_validate.py"
validator_spec = importlib.util.spec_from_file_location("quick_validate", VALIDATOR_PATH)
assert validator_spec is not None and validator_spec.loader is not None
quick_validate = importlib.util.module_from_spec(validator_spec)
validator_spec.loader.exec_module(quick_validate)

SIMULATOR = command_policy_hook.load_simulator()
# The catalog is parsed from every SKILL.md; parse it once for the in-process cases.
SIMULATOR.iter_policies = functools.cache(SIMULATOR.iter_policies)
command_policy_hook.load_simulator = lambda: SIMULATOR


def run_hook(payload: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK), *arguments], input=payload, capture_output=True, text=True
    )


def bash(command: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the hook's entry point in-process; run_hook covers the real process."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd or Path.cwd())})
    stderr = io.StringIO()
    with (
        mock.patch.object(sys, "stdin", io.StringIO(payload)),
        mock.patch.object(sys, "argv", [str(HOOK)]),
        contextlib.redirect_stderr(stderr),
    ):
        returncode = command_policy_hook.main()
    return subprocess.CompletedProcess([str(HOOK)], returncode, "", stderr.getvalue())


def linked_worktree(checkout: Path, linked: Path) -> Path:
    subprocess.run(["git", "-C", str(checkout), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "fixture"], check=True)
    subprocess.run(["git", "-C", str(checkout), "worktree", "add", "--detach", str(linked)], check=True, capture_output=True)
    return linked


class CommandPolicyHookTests(unittest.TestCase):
    def test_repository_exports_are_scoped_to_real_checkouts_and_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "remote.origin.url", "git@github.com:cbusillo/launchplane.git"], check=True)
            frontend = checkout / "frontend"
            frontend.mkdir()
            commands = (
                "launchplane service export-openapi --output generated/openapi.json",
                "launchplane service export-agent-contract --output contracts/agent.json",
                "launchplane service export-owner-control-contract --output contracts/owner.json",
                "launchplane ci unittest-shard local",
                "launchplane ci unittest-shard plan --shard-count 2 --timings-file timings.json",
                "launchplane ci unittest-shard run --shard-count 2 --shard-index 0",
                "launchplane service audit-config-authority --control-plane-root .",
                "launchplane odoo-ownership check --workspace-root ..",
            )
            for command in commands:
                with self.subTest(command=command):
                    self.assertEqual(bash("uv run " + command, checkout).returncode, 0)
                    self.assertEqual(bash("uv run " + command, frontend).returncode, 0)
                    self.assertEqual(bash("uv run --extra dev " + command, checkout).returncode, 0)
                    self.assertEqual(bash("uv run " + command, root).returncode, 2)
                    self.assertEqual(SIMULATOR.simulate(shlex.split(command), cwd=checkout), [])
                    self.assertTrue(SIMULATOR.simulate(shlex.split(command)))
            self.assertEqual(bash("pnpm --dir frontend generate:openapi", checkout).returncode, 0)
            self.assertEqual(bash("launchplane merge-train run-once", checkout).returncode, 2)
            self.assertEqual(bash("launchplane service start", checkout).returncode, 2)
            self.assertEqual(bash("uv run " + commands[0] + " && launchplane service start", checkout).returncode, 2)
            # A real linked worktree, not a mocked directory name or host runtime.
            linked = linked_worktree(checkout, root / "linked")
            self.assertEqual(bash("uv run " + commands[0], linked).returncode, 0)
            for origin in (
                "https://github.com/cbusillo/launchplane.git",
                "ssh://git@github.com/cbusillo/launchplane.git",
                "https://github.com/cbusillo/launchplane",
            ):
                subprocess.run(["git", "-C", str(checkout), "config", "remote.origin.url", origin], check=True)
                self.assertEqual(bash("uv run " + commands[0], checkout).returncode, 0)
            for origin in (
                "https://github.com/other/launchplane.git",
                "https://github.com/cbusillo/launchplane-other.git",
                "https://github.com.evil.invalid/cbusillo/launchplane.git",
                "https://evil.invalid/cbusillo/launchplane.git",
            ):
                subprocess.run(["git", "-C", str(checkout), "config", "remote.origin.url", origin], check=True)
                self.assertEqual(bash("uv run " + commands[0], checkout).returncode, 2)
            subprocess.run(["git", "-C", str(checkout), "config", "--unset", "remote.origin.url"], check=True)
            self.assertEqual(bash("uv run " + commands[0], checkout).returncode, 2)

    def test_repository_exception_cannot_follow_unverified_command_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "remote.origin.url", "https://github.com/cbusillo/launchplane.git"], check=True)
            command = "launchplane service export-openapi --output artifact.json"
            for line in (
                "cd /other && " + command,
                ". /other/setup && uv run " + command,
                "uv run --directory /other " + command,
                "uv run --project=/other " + command,
                "env -C /other " + command,
                "env -C/other " + command,
                "uv run --no-project " + command,
                "uv run --with other-package " + command,
                "UV_PROJECT=/other uv run " + command,
                "/global/bin/" + command,
                "bash -lc 'cd /other && " + command + "'",
                "true && bash -lc 'cd /other && " + command + "'",
                "bash -lc 'true' && cd /other && " + command,
                "uv run --with=other-package " + command,
                "(cd /other && uv run " + command + ")",
                "true&&cd /other&&uv run " + command,
                "true&&bash -lc 'cd /other && uv run " + command + "'",
                command,
            ):
                with self.subTest(line=line):
                    self.assertEqual(bash(line, checkout).returncode, 2)
            self.assertEqual(bash("bash -lc 'uv run " + command + "'", checkout).returncode, 0)
            with mock.patch("subprocess.run", side_effect=OSError("git unavailable")):
                self.assertEqual(bash("uv run " + command, checkout).returncode, 2)
            with mock.patch.dict(os.environ, {"GIT_DIR": "/other", "GIT_WORK_TREE": "/other"}):
                self.assertEqual(bash("uv run " + command, checkout).returncode, 0)
            payload = json.dumps({"tool_name": "Bash", "cwd": str(checkout), "tool_input": {"command": "uv run " + command}})
            self.assertEqual(run_hook(payload).returncode, 0)

    def test_literal_cd_prefix_uses_only_the_verified_target_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "launchplane checkout"
            checkout.mkdir()
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "remote.origin.url", "git@github.com:cbusillo/launchplane.git"], check=True)
            linked = linked_worktree(checkout, root / "linked")
            gate = "uv run --extra dev launchplane ci unittest-shard local"
            export = "uv run launchplane service export-openapi --output artifact.json"
            for target in (checkout, linked):
                for command in (gate, export):
                    for prefix in ("cd ", "cd -- "):
                        line = prefix + shlex.quote(str(target)) + "&&" + command
                        with self.subTest(line=line):
                            self.assertEqual(bash(line, root).returncode, 0)
                            self.assertEqual(bash("bash -lc " + shlex.quote(line), root).returncode, 0)
            for line in (
                "cd " + shlex.quote(str(root)) + " && " + gate,
                "cd /missing-launchplane-checkout && " + gate,
                "cd linked && " + gate,
                "cd '$CHECKOUT' && " + gate,
                "cd " + shlex.quote(str(checkout)) + " ; " + gate,
                "cd " + shlex.quote(str(checkout)) + " || " + gate,
                "cd " + shlex.quote(str(checkout)) + " && false || " + gate,
                "cd " + shlex.quote(str(checkout)) + " && true; " + gate,
                "(cd " + shlex.quote(str(checkout)) + " && " + gate + ")",
                "cd " + shlex.quote(str(checkout)) + " && cd " + shlex.quote(str(root)) + " && " + gate,
                "cd " + shlex.quote(str(checkout)) + " && uv run --project /other launchplane ci unittest-shard local",
                "cd " + shlex.quote(str(checkout)) + " && uv run launchplane service start",
                "cd " + shlex.quote(str(checkout)) + " && " + gate + " && uv run launchplane merge-train run-once",
            ):
                with self.subTest(line=line):
                    self.assertEqual(bash(line, checkout).returncode, 2)

    def test_offline_gate_exceptions_do_not_allow_other_cli_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "remote.origin.url", "git@github.com:cbusillo/launchplane.git"], check=True)
            for command in (
                "launchplane ci postgres-integration",
                "launchplane ci unittest-shard run-targets",
                "launchplane ci unittest-shard list",
                "launchplane ci unittest-shard local-other",
                "launchplane ci other-command",
                "launchplane service audit-config-authority-other",
                "launchplane odoo-ownership other-command",
                "launchplane odoo-targets replacement-plan",
                "launchplane merge-train run-once",
                "launchplane service start",
            ):
                with self.subTest(command=command):
                    self.assertEqual(bash("uv run --extra dev " + command, checkout).returncode, 2)
            self.assertEqual(bash("launchplane ci unittest-shard local", checkout).returncode, 2)
            self.assertEqual(bash("uv run --project /other launchplane ci unittest-shard local", checkout).returncode, 2)

    def test_gate_batches_keep_newline_and_substitution_live_commands_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "config", "remote.origin.url", "git@github.com:cbusillo/launchplane.git"], check=True)
            gate = "uv run --extra dev launchplane ci unittest-shard local"
            live = "uv run launchplane merge-train run-once"
            for line in (
                gate + "\n" + live,
                gate + " # it's fine\n" + live,
                "ls\n" + gate + "\n" + live,
                "cd " + shlex.quote(str(checkout)) + " && " + gate + "\n" + live,
                "cd " + shlex.quote(str(checkout)) + " && " + gate + " # fine\n" + live,
                'uv run launchplane service export-openapi --output "$(' + live + ')"',
                'uv run launchplane service export-openapi --output "`' + live + '`"',
            ):
                with self.subTest(line=line):
                    self.assertEqual(bash(line, checkout).returncode, 2)
            self.assertEqual(bash(gate + "\n", checkout).returncode, 0)
            self.assertEqual(bash("printf '%s' 'literal\ntext'\n" + gate, checkout).returncode, 0)

    def test_shell_comments_preserve_existing_blocks(self) -> None:
        for line in ("gh pr merge 17 # it's green", "# don't bypass\ngh pr merge 17"):
            with self.subTest(line=line):
                self.assertEqual(bash(line).returncode, 2)

    def test_unrelated_commands_do_not_read_repository_identity(self) -> None:
        with mock.patch.dict(vars(SIMULATOR), {"verified_repository": mock.Mock(side_effect=AssertionError("unexpected Git read"))}):
            self.assertIsNone(command_policy_hook.blocking_policy("git status && printf ok"))

    def test_repository_exception_metadata_rejects_ambiguous_or_empty_scopes(self) -> None:
        policy = {"id": "fixture", "match": {"argv_prefix": ["demo"]}, "action": "reject"}
        self.assertIsNone(quick_validate.validate_command_policies([policy]))
        scoped = {"repository": "owner/repo", "argv_prefix": ["demo", "export"]}
        self.assertIsNone(quick_validate.validate_command_policies([{**policy, "exceptions": [scoped]}]))
        for exceptions in (
            "owner/repo", [{}], [{**scoped, "repository": "https://github.com/owner/repo"}],
            [{**scoped, "argv_prefix": []}], [{**scoped, "shell_regex": ".*"}],
            [{**scoped, "argv_prefix": ["demo"]}], [{**scoped, "argv_prefix": ["other", "export"]}],
        ):
            with self.subTest(exceptions=exceptions):
                self.assertIsNotNone(quick_validate.validate_command_policies([{**policy, "exceptions": exceptions}]))

    def test_json_mode_denies_without_using_the_launcher_error_exit_code(self) -> None:
        result = run_hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": "gh-with-env-token pr merge 17"}}), "--json")
        self.assertEqual(result.returncode, 0)
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("Load the `github` skill", decision["permissionDecisionReason"])

    def test_registered_launcher_does_not_turn_uv_failure_into_a_denial(self) -> None:
        definition = json.loads(HOOK.with_name("hooks.json").read_text())
        command = definition["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "uv"
            launcher.write_text("#!/bin/sh\nexit 2\n")
            launcher.chmod(0o700)
            result = subprocess.run(["/bin/sh", "-c", command], env={**os.environ, "PATH": str(root), "CLAUDE_PLUGIN_ROOT": str(root)}, capture_output=True, text=True)
            self.assertEqual((result.returncode, result.stdout), (0, ""))

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

    def test_git_global_options_do_not_hide_commit_or_push(self) -> None:
        for line in (
            "git -c commit.gpgsign=false commit -m demo",
            "cd /tmp && git -C 'a path' commit -m demo",
            "bash -lc 'git --no-pager -C repo commit'",
            "git -c user.name=\"Shiny Code\" commit -m demo",
            "git -C repo push origin branch",
        ):
            with self.subTest(line=line):
                self.assertEqual(bash(line).returncode, 2)
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
