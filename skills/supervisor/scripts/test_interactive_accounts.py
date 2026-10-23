#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise sourced interactive functions with fake Context Panel and harnesses."""

import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import interactive_accounts as accounts

SCRIPT = Path(__file__).with_name("interactive-accounts.zsh")


class InteractiveAccountsTests(unittest.TestCase):
    def test_invalid_custom_prompt_encoding_reports_its_path_before_native_exec(self) -> None:
        custom = self.root / "invalid-encoding.md"
        custom.write_bytes(b"Custom instruction: \xff")
        stderr = io.StringIO()
        with (patch.dict(os.environ, self.env, clear=True),
              patch("runpy.run_path", return_value={"direction_content": lambda *args, **kwargs: "Merged fixture direction."}),
              patch.object(sys, "argv", [str(accounts.__file__), "native", "--", "claude", "--append-system-prompt-file", str(custom)]),
              patch.object(accounts.os, "execvpe") as execute,
              redirect_stderr(stderr), self.assertRaises(SystemExit) as refusal):
            accounts.main()
        self.assertEqual(refusal.exception.code, 1)
        self.assertIn(str(custom), stderr.getvalue())
        execute.assert_not_called()

    def test_reentering_a_generated_prompt_refreshes_direction_without_copying_the_old_payload(self) -> None:
        old, current = "Old fixture direction.", "Current fixture direction."
        contents = iter((old, current))
        with (patch.dict(os.environ, self.env, clear=True),
              patch("runpy.run_path", return_value={"direction_content": lambda *args, **kwargs: next(contents)})):
            first = accounts.prepare_claude_arguments([], self.root)
            prepared = accounts.prepare_claude_arguments(first, self.root)
        first_path = Path(first[first.index("--append-system-prompt-file") + 1])
        current_path = Path(prepared[prepared.index("--append-system-prompt-file") + 1])
        self.assertEqual(first_path.read_text(), old)
        self.assertEqual(current_path.read_text(), current)

    def test_native_text_prompt_survives_direction_preparation(self) -> None:
        for arguments in (["--append-system-prompt", "Always use TypeScript"],
                          ["--append-system-prompt=Always use TypeScript"]):
            with (patch.dict(os.environ, self.env, clear=True),
                  patch("runpy.run_path", return_value={"direction_content": lambda *args, **kwargs: "Merged fixture direction."}),
                  patch.object(accounts.os, "execvpe") as execute):
                accounts.launch("claude", arguments)
            self.assertEqual(execute.call_args.args[1][1:1 + len(arguments)], arguments)

    def test_generated_prompt_replaces_original_file_and_snapshot_options(self) -> None:
        custom = self.root / "custom.md"
        custom.write_text("Custom fixture instruction.")
        arguments = ["--append-system-prompt-file", str(custom), "--system-prompt-snapshot=on", "--", "literal prompt"]
        with (patch.dict(os.environ, self.env, clear=True),
              patch("runpy.run_path", return_value={"direction_content": lambda *args, **kwargs: "Merged fixture direction."}),
              patch.object(accounts.os, "execvpe") as execute):
            accounts.launch("claude", arguments)
        launched = execute.call_args.args[1][1:]
        self.assertEqual(launched.count("--append-system-prompt-file"), 1)
        self.assertNotIn("--system-prompt-snapshot=on", launched)
        self.assertEqual(launched[launched.index("--") + 1:], ["literal prompt"])

    def test_claude_launches_share_a_regular_prompt_file_that_survives_reexec(self) -> None:
        custom = self.root / "custom.md"
        custom.write_text("Custom fixture instruction.")
        for named in (None, "gamma"):
            for path in (str(custom), "custom.md", "~/custom.md"):
                with (patch.dict(os.environ, {**self.env, "FIXTURE_PROMPT": str(custom)}, clear=True),
                      patch("runpy.run_path", return_value={"direction_content": lambda *args, **kwargs: "Merged fixture direction."}),
                      patch.object(accounts.Path, "cwd", return_value=self.root),
                      patch.object(accounts.os, "execvpe") as execute):
                    accounts.launch("claude", ["--append-system-prompt-file", path, "--", "literal prompt"], named)
                arguments = execute.call_args.args[1]
                index = arguments.index("--append-system-prompt-file")
                prompt = Path(arguments[index + 1])
                self.assertTrue(prompt.is_file())
                self.assertIn("Merged fixture direction.", prompt.read_text())
                self.assertIn(custom.read_text(), prompt.read_text())
                self.assertEqual(arguments[arguments.index("--") + 1:], ["literal prompt"])
                for _ in range(2):
                    result = subprocess.run([sys.executable, "-c", "import sys; from pathlib import Path; print(Path(sys.argv[1]).read_text())",
                                             str(prompt)], text=True, capture_output=True, check=True)
                    self.assertIn("Merged fixture direction.", result.stdout)

    def test_claude_subcommands_and_unreadable_prompts_do_not_start_partial_sessions(self) -> None:
        with (patch.dict(os.environ, self.env, clear=True),
              patch("runpy.run_path", return_value={"direction_content": lambda *args, **kwargs: "Merged fixture direction."}) as renderer,
              patch.object(accounts.os, "execvpe") as execute):
            for arguments in (["auth", "status"], ["daemon", "status"], ["mcp", "list"], ["--version"],
                              ["self-hosted-runner", "doctor"], ["--dangerously-skip-permissions", "daemon", "status"],
                              ["--allow-dangerously-skip-permissions", "daemon", "status"], ["--desktop"],
                              ["--bg", "--exec", "printf fixture"]):
                accounts.launch("claude", arguments)
                self.assertEqual(execute.call_args.args[1][1:], arguments)
            renderer.assert_not_called()
            execute.reset_mock()
            missing = self.root / "missing.md"
            with self.assertRaises(ValueError) as failure:
                accounts.launch("claude", ["--append-system-prompt-file", str(missing)])
            self.assertIn(str(missing), str(failure.exception))
            execute.assert_not_called()

    def test_prompt_operands_do_not_hide_direction_or_turn_tail_text_into_options(self) -> None:
        cases = (["--append-system-prompt", "--desktop", "--", "--version"],
                 ["--append-system-prompt", "--", "--model", "fixture", "--", "literal"],
                 ["--append-system-prompt=--desktop", "--", "--append-system-prompt-file", "missing.md"],
                 ["bg"], ["config"], ["--teleport"])
        with (patch.dict(os.environ, self.env, clear=True),
              patch("runpy.run_path", return_value={"direction_content": lambda *args, **kwargs: "Merged fixture direction."})):
            for arguments in cases:
                with self.subTest(arguments=arguments):
                    prepared = accounts.prepare_claude_arguments(arguments, self.root)
                    index = next(i for i, value in enumerate(prepared)
                                 if value.partition("=")[0] == "--append-system-prompt-file")
                    option, equals, value = prepared[index].partition("=")
                    prompt = Path(value if equals else prepared[index + 1])
                    self.assertTrue(prompt.is_file())

    def test_prompt_file_equals_form_and_literal_variable_path(self) -> None:
        custom = self.root / "$FIXTURE_PROMPT"
        custom.write_text("Literal variable filename.")
        with (patch.dict(os.environ, {**self.env, "FIXTURE_PROMPT": "missing.md"}, clear=True),
              patch("runpy.run_path", return_value={"direction_content": lambda *args, **kwargs: "Merged fixture direction."})):
            prepared = accounts.prepare_claude_arguments([f"--append-system-prompt-file={custom.name}"], self.root)
        self.assertIn(custom.read_text(), Path(prepared[prepared.index("--append-system-prompt-file") + 1]).read_text())

    def test_required_directory_operand_is_not_a_prompt_delimiter(self) -> None:
        (self.root / "--").mkdir()
        arguments = ["--add-dir", "--", "--", "literal prompt"]
        with (patch.dict(os.environ, self.env, clear=True),
              patch("runpy.run_path", return_value={"direction_content": lambda *args, **kwargs: "Merged fixture direction."})):
            prepared = accounts.prepare_claude_arguments(arguments, self.root)
        self.assertEqual(prepared[:2], arguments[:2])
        self.assertEqual(prepared[-2:], arguments[-2:])

    def test_missing_option_values_and_cache_write_failure_are_launch_errors(self) -> None:
        with (patch.dict(os.environ, {**self.env, "CODE_HOME": str(self.root / "code")}, clear=True),
              patch("runpy.run_path", return_value={"direction_content": lambda *args, **kwargs: "Merged fixture direction."})):
            for arguments in (["--append-system-prompt-file"], ["--append-system-prompt"], ["--system-prompt-snapshot"]):
                with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, arguments[0]):
                    accounts.prepare_claude_arguments(arguments, self.root)
            with patch.object(accounts, "write_cache", side_effect=OSError("fixture disk full")) as write:
                with self.assertRaises(ValueError) as failure:
                    accounts.prepare_claude_arguments([], self.root)
            self.assertIn(str(write.call_args.args[0]), str(failure.exception))
            self.assertIn("fixture disk full", str(failure.exception))
            with patch("runpy.run_path", side_effect=SyntaxError("fixture parse failure")) as read:
                with self.assertRaises(ValueError) as invalid_renderer:
                    accounts.prepare_claude_arguments([], self.root)
            self.assertIn(str(read.call_args.args[0]), str(invalid_renderer.exception))

    def test_no_direction_keeps_native_prompt_options_unchanged(self) -> None:
        arguments = ["--append-system-prompt-file", "missing.md", "--system-prompt-snapshot=on", "--", "literal"]
        with patch("runpy.run_path", return_value={"direction_content": lambda *args, **kwargs: ""}):
            self.assertEqual(accounts.prepare_claude_arguments(arguments, self.root), arguments)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.snapshot = self.root / "snapshot.json"
        self.snapshot.write_text(json.dumps({
            "schemaVersion": 1,
            "accounts": [{"provider": "openai", "id": "openai-a", "label": "Alpha"},
                         {"provider": "openai", "id": "openai-b", "label": "Beta"},
                         {"provider": "anthropic", "id": "anthropic-c", "label": "Gamma"}],
            "answers": {"useNext": [{"provider": "openai", "accountID": "openai-b"},
                                    {"provider": "anthropic", "accountID": "anthropic-c"}]}}))
        config_dir = self.root / ".code/skill-data"
        config_dir.mkdir(parents=True)
        (config_dir / "supervisor.toml").write_text(
            '[accounts]\nsnapshot_command = ["cat", ' + json.dumps(str(self.snapshot)) + ']\n' +
            '\n'.join(f'[[accounts.account]]\nname = "{name}"\nprovider = "{provider}"\n'
                      f'context_panel_label = "{label}"\n{settings}\n'
                      for name, provider, label, settings in [
                          ("alpha", "openai", "Alpha", 'env = { CODEX_HOME = "~/alpha home" }'),
                          ("beta", "openai", "Beta", 'env = { CODEX_HOME = "~/beta" }'),
                          ("gamma", "anthropic", "Gamma", 'env = {}')]))
        for harness in accounts.HARNESSES:
            binary = self.bin / harness
            binary.write_text(f'#!{sys.executable}\nimport json, os, sys\n'
                              'print(json.dumps({"argv":sys.argv[1:], "home":os.getenv("CODEX_HOME"), '
                              '"claude":os.getenv("CLAUDE_CONFIG_DIR")}))\nsys.exit(int(os.getenv("FAKE_EXIT", "0")))\n')
            binary.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in {"CODEX_HOME", "CLAUDE_CONFIG_DIR", "CODE_HOME"}}
        self.env.update(HOME=str(self.root), XDG_CACHE_HOME=str(self.root / "cache"),
                        PATH=str(self.bin) + os.pathsep + os.environ["PATH"])

    def run_shell(self, invocation, *, interactive=True, env=None):
        return subprocess.run([shutil.which("zsh"), "-dfi" if interactive else "-df",
                               "-c", 'source "$1"; ' + invocation, "fixture", str(SCRIPT)],
                              cwd=self.root, env={**self.env, **(env or {})}, text=True, capture_output=True,
                              timeout=30)

    def launched(self, invocation, **kwargs):
        result = self.run_shell(invocation, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        return json.loads(result.stdout), result.stderr

    def test_next_and_native_arguments(self):
        arguments = ["--resume", "session id", "--continue", "a'b", "$(echo unsafe)", "", "--", "--account", "literal"]
        launched, _ = self.launched("codex " + shlex.join(arguments))
        self.assertEqual(launched["home"], str(self.root / "beta"))
        self.assertEqual(launched["argv"], arguments)
        launched, _ = self.launched("claude --continue")
        self.assertIsNone(launched["claude"])
        self.assertEqual(launched["argv"], ["--continue"])

    def test_explicit_environment_does_not_query_snapshot(self):
        self.snapshot.unlink()
        launched, _ = self.launched("codex --resume", env={"CODEX_HOME": str(self.root / "explicit")})
        self.assertEqual(launched["home"], str(self.root / "explicit"))

    def test_named_and_account_flag_override_environment(self):
        self.snapshot.unlink()
        override = {"CODEX_HOME": str(self.root / "inherited")}
        for invocation in ["codex-alpha --resume", "codex --account alpha --resume", "codex --account=alpha --resume"]:
            launched, _ = self.launched(invocation, env=override)
            self.assertEqual(launched["home"], str(self.root / "alpha home"))
            self.assertEqual(launched["argv"], ["--resume"])
        launched, _ = self.launched("claude-gamma --continue", env={"CLAUDE_CONFIG_DIR": "/inherited"})
        self.assertIsNone(launched["claude"])

    def test_unavailable_and_stale_fall_back_to_last_used(self):
        self.launched("codex --account alpha")
        self.snapshot.unlink()
        launched, _ = self.launched("codex --continue")
        self.assertEqual(launched["home"], str(self.root / "alpha home"))
        self.snapshot.write_text(json.dumps({"schemaVersion": 1, "accounts": [], "ranking": [
            {"provider": "openai", "basedOnStaleReadings": True, "readingsObservedAt": "2000-01-01T00:00:00Z"}]}))
        launched, _ = self.launched("codex")
        self.assertEqual(launched["home"], str(self.root / "alpha home"))

    def test_first_launch_without_snapshot_uses_native_default(self):
        self.snapshot.unlink()
        launched, _ = self.launched("codex")
        self.assertIsNone(launched["home"])

    def test_generated_variants_and_functions_only(self):
        result = self.run_shell("whence -w codex claude codex-alpha codex-beta claude-gamma; command codex --resume")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertTrue(all(line.endswith(": function") for line in lines[:-1]), lines)
        self.assertEqual(len(lines[:-1]), 5)
        self.assertIsNone(json.loads(lines[-1])["home"])
        self.assertEqual(result.stderr, "")
        result = self.run_shell("whence -w codex; codex --continue", interactive=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("codex: command", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_native_exit_status_and_invalid_account(self):
        result = self.run_shell("codex", env={"FAKE_EXIT": "7"})
        self.assertEqual(result.returncode, 7)
        result = self.run_shell("codex --account unknown --resume")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_unsafe_function_name_is_rejected(self):
        with self.assertRaises(ValueError):
            accounts.variants({"accounts": [{"name": "bad;command", "provider": "openai"}]})


if __name__ == "__main__":
    unittest.main()
