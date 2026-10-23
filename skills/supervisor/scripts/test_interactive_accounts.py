#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise sourced interactive functions with fake Context Panel and harnesses."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import interactive_accounts as accounts

SCRIPT = Path(__file__).with_name("interactive-accounts.zsh")


class InteractiveAccountsTests(unittest.TestCase):
    def test_claude_launches_share_a_regular_prompt_file_that_survives_reexec(self) -> None:
        custom = self.root / "custom.md"
        custom.write_text("Custom fixture instruction.")
        for named in (None, "gamma"):
            for path in (str(custom), "custom.md", "~/custom.md", "$FIXTURE_PROMPT"):
                with (patch.dict(os.environ, {**self.env, "FIXTURE_PROMPT": str(custom)}, clear=True),
                      patch("runpy.run_path", return_value={"direction_content": lambda *args, **kwargs: "Merged fixture direction."}),
                      patch.object(accounts.Path, "cwd", return_value=self.root),
                      patch.object(accounts.os, "execvpe") as execute):
                    accounts.launch("claude", ["--append-system-prompt-file", path, "--", "literal prompt"], named)
                arguments = execute.call_args.args[1]
                index = max(i for i, value in enumerate(arguments) if value == "--append-system-prompt-file")
                prompt = Path(arguments[index + 1])
                self.assertTrue(prompt.is_file())
                self.assertFalse(str(prompt).startswith("/dev/fd/"))
                self.assertIn("Merged fixture direction.", prompt.read_text())
                self.assertIn(custom.read_text(), prompt.read_text())
                self.assertEqual(arguments[arguments.index("--") + 1:], ["literal prompt"])
                for _ in range(2):
                    result = subprocess.run([sys.executable, "-c", "import sys; from pathlib import Path; print(Path(sys.argv[1]).read_text())",
                                             str(prompt)], text=True, capture_output=True, check=True)
                    self.assertIn("Merged fixture direction.", result.stdout)

    def test_claude_subcommands_and_unreadable_prompts_do_not_start_partial_sessions(self) -> None:
        with (patch.dict(os.environ, self.env, clear=True),
              patch("runpy.run_path", return_value={"direction_content": lambda *args, **kwargs: "Merged fixture direction."}),
              patch.object(accounts.os, "execvpe") as execute):
            for arguments in (["auth", "status"], ["daemon", "status"], ["mcp", "list"], ["--version"]):
                accounts.launch("claude", arguments)
                self.assertEqual(execute.call_args.args[1][1:], arguments)
            execute.reset_mock()
            with self.assertRaises(OSError):
                accounts.launch("claude", ["--append-system-prompt-file", str(self.root / "missing.md")])
            execute.assert_not_called()

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
