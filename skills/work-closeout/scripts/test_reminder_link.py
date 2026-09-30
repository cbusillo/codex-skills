#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline behavioral tests; no host configuration or Reminders access."""
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import reminder_link

from reminder_link import prepare, target_list, require_unique_list, verify_native_list

SESSION = "12345678-1234-1234-1234-123456789abc"


class ReminderLinkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "repo ' & $(touch bad);é"
        self.directory.mkdir()

    def test_resume_round_trip_both_harnesses(self):
        for harness, argument in (("codex", "resume"), ("claude", "--resume")):
            result = prepare(harness, self.directory, SESSION, None)
            query = parse_qs(urlsplit(result["url"]).query)
            self.assertEqual(shlex.split(query["c"][0]), [harness, argument, SESSION])
            self.assertEqual(query["d"], [str(self.directory.resolve())])
            self.assertEqual(set(query), {"c", "d"})
            self.assertEqual(urlsplit(result["url"]).netloc, "")

    def test_fresh_prompt_is_one_argument_without_shell_execution(self):
        for harness in ("codex", "claude"):
            prompt = "--search ' quote & $(touch bad); `touch bad`\né"
            result = prepare(harness, self.directory, None, prompt)
            query = parse_qs(urlsplit(result["url"]).query)
            self.assertEqual(shlex.split(query["c"][0]), [harness, "--", prompt])
            # Evaluate fallback quoting with a harmless shell function, no model process.
            output = subprocess.check_output(["sh", "-c", f'{harness}() {{ printf "%s" "$2"; }}; ' + result["fallback"]], text=True)
            self.assertEqual(output, prompt)
            self.assertFalse((self.directory / "bad").exists())

    def test_invalid_session_and_modes_fail(self):
        for session in ("--last", "latest", "$(date)", "", "123"):
            with self.assertRaises(ValueError):
                prepare("codex", self.directory, session, None)
        for session, prompt in ((None, None), (SESSION, "both"), (None, ""), (None, "x\x00y")):
            with self.assertRaises(ValueError):
                prepare("claude", self.directory, session, prompt)
        with self.assertRaises(FileNotFoundError):
            prepare("codex", self.root / "gone", SESSION, None)

    def test_marker_changes_only_with_target(self):
        first = prepare("codex", self.directory, SESSION, None)
        self.assertEqual(first["marker"], prepare("codex", self.directory, SESSION.upper(), None)["marker"])
        other = self.root / "other"
        other.mkdir()
        for result in (prepare("claude", self.directory, SESSION, None),
                       prepare("codex", other, SESSION, None),
                       prepare("codex", self.directory, None, "fresh"),
                       prepare("codex", self.directory, None, SESSION)):
            self.assertNotEqual(first["marker"], result["marker"])

    @staticmethod
    def config(root, name):
        path = root / "skill-data/work-closeout.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[reminders]\nlist = "' + name + '"\n')
        return path

    def test_private_resolution_override_repo_user_and_home(self):
        code = self.root / "code"
        codex = self.root / "codex"
        home = self.root / "home"
        self.config(home / ".code", "home")
        codex_file = self.config(codex, "codex")
        code_file = self.config(code, "code")
        repo_file = self.config(self.directory / ".local", "repo")
        env = {"CODE_HOME": str(code), "CODEX_HOME": str(codex)}
        for override, expected in (("one-off", "one-off"), (None, "repo")):
            self.assertEqual(target_list(self.directory, override, env, home), expected)
        repo_file.unlink()
        self.assertEqual(target_list(self.directory, None, env, home), "code")
        code_file.write_text("[other]\nvalue = 1")
        self.assertEqual(target_list(self.directory, None, env, home), "codex")
        codex_file.unlink()
        self.assertEqual(target_list(self.directory, None, env, home), "home")

    def test_invalid_config_and_missing_list_do_not_fall_back(self):
        home = self.root / "home"
        with self.assertRaises(ValueError):
            target_list(self.directory, None, {}, home)
        path = self.config(self.directory / ".local", "")
        for content in ('[reminders]\nlist = ""', '[reminders]\nlist = 3', 'malformed ['):
            path.write_text(content)
            with self.assertRaises(ValueError):
                target_list(self.directory, None, {}, home)
        for titles in ([], ["renamed"], ["target", "target"]):
            with self.assertRaises(ValueError):
                require_unique_list("target", titles)
        require_unique_list("target", ["another", "target"])

    def test_native_lookup_failure_is_not_success(self):
        with patch.object(reminder_link.sys, "platform", "darwin"), patch.object(reminder_link.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, "", "access unavailable")
            with self.assertRaisesRegex(ValueError, "access unavailable"):
                verify_native_list("target")
            run.return_value = subprocess.CompletedProcess([], 0, '["target", "target"]', "")
            with self.assertRaisesRegex(ValueError, "2 exact matches"):
                verify_native_list("target")
            run.return_value = subprocess.CompletedProcess([], 0, '["target"]', "")
            verify_native_list("target")
            self.assertEqual(run.call_args.args[0][-1], "target")
            self.assertFalse(run.call_args.kwargs.get("shell", False))


if __name__ == "__main__":
    unittest.main()
