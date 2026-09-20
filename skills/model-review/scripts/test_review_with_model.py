#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Run review_with_model.py against fake provider CLIs; no model access is needed."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("review_with_model.py")

FAKE_CODEX = """#!/bin/sh
# Writes the answer to the file given after -o, like `codex exec`.
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
echo "model: gpt-test"
printf '%s' "$FAKE_ANSWER" > "$out"
"""
FAKE_CLAUDE = """#!/bin/sh
[ -n "$FAKE_CLAUDE_ARGV_FILE" ] && printf '%s\\n' "$@" > "$FAKE_CLAUDE_ARGV_FILE"
printf '%s' "$FAKE_CLAUDE_JSON"
"""
FAKE_AGY = """#!/bin/sh
pwd > "$FAKE_AGY_CWD_FILE"
echo "jetski: some banner text"
printf '%s' "$FAKE_AGY_JSON"
"""


class ReviewWithModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.bin, self.home, self.repo = self.root / "bin", self.root / "home", self.root / "work" / "repo"
        for directory in (self.bin, self.home, self.repo):
            directory.mkdir(parents=True)
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("Review skills/x.md.\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def install(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def run_helper(self, *args: str, **env: str) -> tuple[int, dict]:
        # PATH holds only the fakes plus the interpreter's directory, so an uninstalled provider stays uninstalled.
        environment = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), **env}
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=environment
        )
        return proc.returncode, json.loads(proc.stdout)

    def review(self, provider: str, **env: str) -> tuple[int, dict]:
        return self.run_helper(
            "run", "--provider", provider, "--repo", str(self.repo), "--prompt-file", str(self.prompt), **env
        )

    def test_each_provider_returns_its_review_and_the_model_that_ran(self) -> None:
        self.install("codex", FAKE_CODEX)
        self.install("claude", FAKE_CLAUDE)
        self.install("agy", FAKE_AGY)
        settings = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"model": "gemini-test"}))
        claude_json = json.dumps({"is_error": False, "result": "none", "modelUsage": {"claude-test": {}}})
        agy_json = json.dumps({"response": "none", "denied_actions": []})
        cwd_file = self.root / "agy-cwd"
        for provider, model, env in (
            ("openai", "gpt-test", {"FAKE_ANSWER": "none"}),
            ("anthropic", "claude-test", {"FAKE_CLAUDE_JSON": claude_json}),
            ("google", "gemini-test", {"FAKE_AGY_JSON": agy_json, "FAKE_AGY_CWD_FILE": str(cwd_file)}),
        ):
            with self.subTest(provider=provider):
                code, result = self.review(provider, **env)
                self.assertEqual((code, result["ok"], result["model"], result["response"]), (0, True, model, "none"))
        # The Google reviewer runs from a scratch directory, never from inside the repository it reads.
        self.assertNotIn(str(self.repo), cwd_file.read_text())
        # agy does not say which model ran, so the result must not pretend it did.
        self.assertIn("not reported", result["model_source"])

    def test_the_anthropic_reviewer_is_given_only_read_tools(self) -> None:
        self.install("claude", FAKE_CLAUDE)
        argv_file = self.root / "claude-argv"
        ok = json.dumps({"is_error": False, "result": "none", "modelUsage": {"claude-test": {}}})
        self.review("anthropic", FAKE_CLAUDE_JSON=ok, FAKE_CLAUDE_ARGV_FILE=str(argv_file))
        argv = argv_file.read_text().split("\n")
        # `--allowedTools` only extends a user's own allowlist; `--tools` is what removes the write tools.
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertNotIn("--allowedTools", argv)

    def test_a_reviewer_that_could_write_or_was_denied_a_read_is_a_failure(self) -> None:
        self.install("claude", FAKE_CLAUDE)
        self.install("agy", FAKE_AGY)
        denied = json.dumps({"is_error": False, "result": "I could not open it.", "permission_denials": [{"tool_name": "Read"}]})
        code, result = self.review("anthropic", FAKE_CLAUDE_JSON=denied)
        self.assertEqual((code, result["denied"]), (1, ["Read"]))
        settings = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"permissions": {"allow": [f"write_file({self.repo})", "command(ls)"]}}))
        cwd_file = self.root / "agy-cwd"
        code, result = self.review("google", FAKE_AGY_JSON="{}", FAKE_AGY_CWD_FILE=str(cwd_file))
        self.assertEqual((code, result["rules"]), (1, [f"write_file({self.repo})"]))
        self.assertFalse(cwd_file.exists(), "agy must not be started when it could write")

    def test_check_probes_any_text_file_and_does_not_pass_when_nothing_is_usable(self) -> None:
        (self.repo / "a.md").write_text("\n  \n")
        (self.repo / "b.txt").write_text("\nthe probe line\n")
        code, report = self.run_helper("check", "--repo", str(self.repo))
        self.assertEqual((code, report["probe"]), (1, str(self.repo / "b.txt")))
        self.assertEqual({item["state"] for item in report["providers"]}, {"not installed"})
        self.install("codex", FAKE_CODEX)
        code, report = self.run_helper("check", "--repo", str(self.repo), FAKE_ANSWER="the probe line")
        self.assertEqual(code, 0)
        self.assertIn("ready", {item["state"] for item in report["providers"]})

    def test_a_locked_out_reviewer_is_a_failure_that_says_how_to_fix_it(self) -> None:
        self.install("agy", FAKE_AGY)
        agy_json = json.dumps({"response": "", "denied_actions": [{"action": "read_file"}]})
        code, result = self.review("google", FAKE_AGY_JSON=agy_json, FAKE_AGY_CWD_FILE=str(self.root / "cwd"))
        self.assertEqual((code, result["ok"], result["denied"]), (1, False, ["read_file"]))
        # The suggested rule names this repository, not its parent: the parent could be a home directory.
        self.assertIn(f"read_file({self.repo})", result["hint"])
        self.assertNotIn(f"read_file({self.repo.parent})\"", result["hint"])
        self.assertIn("configure --read-root", result["hint"])
        self.assertFalse((self.home / ".gemini").exists(), "a failed run must not create or edit user settings")

    def test_an_empty_answer_or_a_provider_error_is_never_reported_as_no_findings(self) -> None:
        self.install("codex", FAKE_CODEX)
        self.install("claude", FAKE_CLAUDE)
        code, result = self.review("openai", FAKE_ANSWER="  \n")
        self.assertEqual((code, result["ok"], result["error"]), (1, False, "the reviewer returned nothing"))
        limit = json.dumps({"is_error": True, "result": "You've hit your monthly spend limit"})
        code, result = self.review("anthropic", FAKE_CLAUDE_JSON=limit)
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn("spend limit", result["detail"])

    def test_a_provider_that_is_not_installed_is_distinguished_from_one_that_failed(self) -> None:
        code, result = self.review("google")
        self.assertEqual((code, result["installed"]), (2, False))

    def test_configure_adds_rules_once_keeps_other_settings_and_leaves_a_backup(self) -> None:
        settings = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"model": "gemini-test", "permissions": {"allow": ["command(ls)"]}}))
        code, result = self.run_helper("configure", "--read-root", str(self.repo.parent))
        self.assertEqual(code, 0)
        written = json.loads(settings.read_text())
        self.assertEqual(written["model"], "gemini-test")
        self.assertIn(f"read_file({self.repo.parent})", written["permissions"]["allow"])
        self.assertEqual(written["permissions"]["allow"].count("command(ls)"), 1)
        self.assertNotIn("command(ls)", result["added"])
        self.assertTrue(settings.with_name("settings.json.before-model-review").is_file())
        code, again = self.run_helper("configure", "--read-root", str(self.repo.parent))
        self.assertEqual((code, again["added"]), (0, []))


if __name__ == "__main__":
    unittest.main()
