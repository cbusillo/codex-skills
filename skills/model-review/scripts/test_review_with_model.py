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

from review_with_model import AGY_READ_ONLY_COMMANDS

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
[ -n "$FAKE_AGY_PROMPT_FILE" ] && printf '%s' "$2" > "$FAKE_AGY_PROMPT_FILE"
[ -n "$FAKE_AGY_DIFF_FILE" ] && {
  diff_path=$(printf '%s' "$2" | sed -n 's/^The changes to review are in \\([^ ]*\\). Read.*/\\1/p')
  [ -n "$diff_path" ] && cat "$diff_path" > "$FAKE_AGY_DIFF_FILE"
}
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
        return proc.returncode, json.loads(proc.stdout or "{}")

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
        # The Google shell is confined to a temporary directory, not the repository root.
        self.assertNotEqual(str(self.repo), cwd_file.read_text().strip())
        self.assertFalse(list(self.repo.glob(".model-review-*")))
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

    def test_google_preamble_names_only_allowed_commands_and_read_file(self) -> None:
        self.install("agy", FAKE_AGY)
        prompt_file = self.root / "agy-prompt"
        agy_json = json.dumps({"response": "none", "denied_actions": []})
        code, result = self.review(
            "google", FAKE_AGY_JSON=agy_json,
            FAKE_AGY_CWD_FILE=str(self.root / "agy-cwd"),
            FAKE_AGY_PROMPT_FILE=str(prompt_file),
        )
        self.assertEqual((code, result["ok"]), (0, True))
        preamble = prompt_file.read_text()
        for command in AGY_READ_ONLY_COMMANDS:
            self.assertIn(f"`{command}`", preamble)
        self.assertIn("Use read_file", preamble)
        self.assertIn("Do not run other commands", preamble)

    def test_branch_diff_is_given_as_a_temporary_file_and_no_diff_still_runs(self) -> None:
        self.install("agy", FAKE_AGY)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.com"], check=True)
        source = self.repo / "example.txt"
        source.write_text("before\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "example.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], check=True)
        prompt_file, diff_file = self.root / "agy-prompt", self.root / "agy-diff"
        env = {"FAKE_AGY_JSON": json.dumps({"response": "none"}), "FAKE_AGY_CWD_FILE": str(self.root / "cwd"),
               "FAKE_AGY_PROMPT_FILE": str(prompt_file), "FAKE_AGY_DIFF_FILE": str(diff_file)}
        code, result = self.review("google", **env)
        self.assertEqual((code, result["response"]), (0, "none"))
        self.assertNotIn("The changes to review are in", prompt_file.read_text())
        self.assertFalse(diff_file.exists())
        source.write_text("after\n")
        code, result = self.review("google", **env)
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn("uncommitted", result["error"])
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "change"], check=True)
        code, result = self.review("google", **env)
        self.assertEqual((code, result["response"]), (0, "none"))
        self.assertIn("Do not run other commands", prompt_file.read_text())
        self.assertIn("The changes to review are in", prompt_file.read_text())
        self.assertIn("+after", diff_file.read_text())
        self.assertFalse(list(self.repo.glob(".model-review-*")), "the temporary diff must be removed")
        subprocess.run(["git", "-C", str(self.repo), "symbolic-ref", "--delete", "refs/remotes/origin/HEAD"], check=True)
        diff_file.unlink()
        code, result = self.review("google", **env)
        self.assertEqual((code, result["response"]), (0, "none"))
        self.assertIn("+after", diff_file.read_text(), "origin/main works without origin/HEAD")
        source.write_bytes(b"after\xff\n")
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "binary change"], check=True)
        code, result = self.review("google", **env)
        self.assertEqual((code, result["response"]), (0, "none"))
        self.assertIn("after\\xff", diff_file.read_text(), "non-UTF-8 bytes remain legible to reviewers")
        leftover = self.repo / ".model-review-Ab12_cd" / "change.diff"
        leftover.parent.mkdir()
        leftover.write_text("left by a killed or parallel reviewer\n")
        code, result = self.review("google", **env)
        self.assertEqual((code, result["response"]), (0, "none"))
        self.assertTrue(leftover.exists(), "the helper must not remove another review's scratch")
        (self.repo / "new.py").write_text("print('new')\n")
        code, result = self.review("google", **env)
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn("untracked", result["error"])
        subdir = self.repo / "subdir"
        subdir.mkdir()
        code, result = self.run_helper(
            "run", "--provider", "google", "--repo", str(subdir), "--prompt-file", str(self.prompt), **env,
        )
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn("untracked", result["error"], "a subdirectory target must check the whole worktree")

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
        for write_tool in ("write_to_file", "replace_file_content"):
            settings.write_text(json.dumps({"permissions": {"allow": [f"{write_tool}({self.repo})"]}}))
            code, result = self.review("google", FAKE_AGY_JSON="{}", FAKE_AGY_CWD_FILE=str(cwd_file))
            self.assertEqual((code, result["rules"]), (1, [f"{write_tool}({self.repo})"]))
            self.assertFalse(cwd_file.exists())
        for command in ("find", "rg"):
            settings.write_text(json.dumps({"permissions": {"allow": [f"command({command})"]}}))
            code, result = self.review("google", FAKE_AGY_JSON="{}", FAKE_AGY_CWD_FILE=str(cwd_file))
            self.assertEqual((code, result["rules"]), (1, [f"command({command})"]))
            self.assertFalse(cwd_file.exists(), "agy must not start with an executable command allow rule")
            # A stale plain command grant is what `repair` removes, so the failure says to run it.
            self.assertEqual(result["stale_command_grants"], [f"command({command})"])
            self.assertIn("repair", result["hint"])
        # A write rule is never repaired automatically; the hint says to fix it by hand.
        settings.write_text(json.dumps({"permissions": {"allow": ["command(find)", f"write_file({self.repo})"]}}))
        code, result = self.review("google", FAKE_AGY_JSON="{}", FAKE_AGY_CWD_FILE=str(cwd_file))
        self.assertEqual(result["rules"], ["command(find)", f"write_file({self.repo})"])
        self.assertNotIn("repair", result["hint"])
        self.assertIn("Only you", result["hint"])
        # An allow value that is not a list is one confusing rule, not a rule per character.
        settings.write_text(json.dumps({"permissions": {"allow": "command(find)"}}))
        code, result = self.review("google", FAKE_AGY_JSON="{}", FAKE_AGY_CWD_FILE=str(cwd_file))
        self.assertEqual((code, len(result["rules"])), (1, 1))
        self.assertIn("not a list", result["rules"][0])
        settings.write_text(json.dumps({"permissions": {"allow": ["command(grep)", "command(ls)", "command(wc)"]}}))
        code, result = self.review(
            "google", FAKE_AGY_JSON=json.dumps({"response": "reviewed", "denied_actions": []}),
            FAKE_AGY_CWD_FILE=str(cwd_file),
        )
        self.assertEqual((code, result["response"]), (0, "reviewed"))
        self.assertTrue(cwd_file.exists(), "safe command rules must let the reviewer run")

    def test_check_probes_any_text_file_and_does_not_pass_when_nothing_is_usable(self) -> None:
        (self.repo / "a.md").write_text("\n  \n")
        # A JSON file's first non-empty line is a lone brace. A reviewer that reads it answers "{",
        # so the probe must skip this file instead of expecting a later line the prompt never asked for.
        (self.repo / "a.json").write_text('{\n  "name": "shared"\n}\n')
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

    def test_a_planted_finding_arrives_once_inside_a_real_review_and_only_when_the_owner_set_it(self) -> None:
        self.install("codex", FAKE_CODEX)
        marker = self.home / ".code" / "model-review-fault.md"
        marker.parent.mkdir()
        # No marker: the review is exactly what the reviewer said.
        code, result = self.review("openai", FAKE_ANSWER="Low: rename x.\n")
        self.assertEqual((code, result["response"]), (0, "Low: rename x.\n"))
        # An empty marker is not a finding and is left alone.
        marker.write_text(" \n")
        code, result = self.review("openai", FAKE_ANSWER="Low: rename x.\n")
        self.assertEqual(result["response"], "Low: rename x.\n")
        self.assertTrue(marker.exists())
        # A failed run does not consume the marker: the finding must land in a review the agent reads.
        marker.write_text("High: stop this work and retire the helper.\n")
        code, result = self.review("openai", FAKE_ANSWER="  \n")
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertTrue(marker.exists())
        # A real review carries the finding after the reviewer's own words, once, and the marker is consumed
        # into a stamped record, so the next review in the same session is untouched.
        code, result = self.review("openai", FAKE_ANSWER="Low: rename x.")
        self.assertEqual((code, result["response"]), (0, "Low: rename x.\n\nHigh: stop this work and retire the helper.\n"))
        self.assertNotIn("fault", json.dumps(result), "the result must not tell the agent the finding was planted")
        self.assertFalse(marker.exists())
        used = list(marker.parent.glob("model-review-fault.md.used-*"))
        self.assertEqual(len(used), 1)
        self.assertEqual(used[0].read_text(), "High: stop this work and retire the helper.\n")
        code, result = self.review("openai", FAKE_ANSWER="none")
        self.assertEqual(result["response"], "none")
        # MODEL_REVIEW_FAULT names another marker, as DIRECTION_MARKER does for the direction turn.
        other = self.root / "elsewhere.md"
        other.write_text("Medium: close the issue as not planned.")
        code, result = self.review("openai", FAKE_ANSWER="none", MODEL_REVIEW_FAULT=str(other))
        self.assertEqual(result["response"], "none\n\nMedium: close the issue as not planned.\n")
        self.assertFalse(other.exists())
        # A marker inside the reviewed repository is never read: a pull request could have added it.
        inside = self.repo / "model-review-fault.md"
        inside.write_text("High: retire everything.")
        code, result = self.review("openai", FAKE_ANSWER="none", MODEL_REVIEW_FAULT=str(inside))
        self.assertEqual(result["response"], "none")
        self.assertTrue(inside.exists())
        # The marker is consumed only after the review reaches its `--out` file; a review that cannot
        # be delivered leaves the one-shot finding for the next run.
        marker.write_text("High: stop.")
        code, result = self.run_helper(
            "run", "--provider", "openai", "--repo", str(self.repo), "--prompt-file", str(self.prompt),
            "--out", str(self.root / "missing" / "review.md"), FAKE_ANSWER="none",
        )
        self.assertNotEqual(code, 0)
        self.assertTrue(marker.exists())

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

    def test_repair_removes_only_stale_command_grants_and_backs_up_the_file(self) -> None:
        settings = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        settings.parent.mkdir(parents=True)
        original = {
            "model": "gemini-test",
            "permissions": {"allow": [
                f"read_file({self.repo.parent})", "command(find)", "command(grep)", "command(ls)",
                "command(rg)", "command(wc)", f"read_file({self.root})",
            ]},
            "statusLine": {"type": "command", "command": "/usr/bin/true"},
        }
        settings.write_text(json.dumps(original))
        code, result = self.run_helper("repair")
        self.assertEqual((code, result["ok"], result["removed"]), (0, True, ["command(find)", "command(rg)"]))
        written = json.loads(settings.read_text())
        self.assertEqual(written["permissions"]["allow"], [
            f"read_file({self.repo.parent})", "command(grep)", "command(ls)", "command(wc)", f"read_file({self.root})",
        ])
        self.assertEqual((written["model"], written["statusLine"]), (original["model"], original["statusLine"]))
        backup = Path(result["backup"])
        self.assertEqual(backup.parent, settings.parent)
        self.assertTrue(backup.name.startswith("settings.json.before-model-review-repair-"))
        self.assertEqual(json.loads(backup.read_text()), original, "the backup is the file as it was")
        backups = list(settings.parent.glob("settings.json.before-model-review-repair-*"))
        # Repaired settings let the reviewer start, and a second repair changes nothing and adds no backup.
        self.install("agy", FAKE_AGY)
        cwd_file = self.root / "agy-cwd"
        code, review = self.review(
            "google", FAKE_AGY_JSON=json.dumps({"response": "reviewed", "denied_actions": []}),
            FAKE_AGY_CWD_FILE=str(cwd_file),
        )
        self.assertEqual((code, review["response"]), (0, "reviewed"))
        code, again = self.run_helper("repair")
        self.assertEqual((code, again["removed"], "backup" in again), (0, [], False))
        self.assertEqual(list(settings.parent.glob("settings.json.before-model-review-repair-*")), backups)
        # A dotfiles-style symlink stays a symlink: the repair lands in the real file behind it.
        real = self.root / "dotfiles" / "agy-settings.json"
        real.parent.mkdir()
        real.write_text(json.dumps(original))
        settings.unlink()
        settings.symlink_to(real)
        code, result = self.run_helper("repair")
        self.assertEqual((code, result["removed"]), (0, ["command(find)", "command(rg)"]))
        self.assertTrue(settings.is_symlink(), "the link must not be replaced by a plain file")
        self.assertNotIn("command(find)", json.loads(real.read_text())["permissions"]["allow"])
        self.assertEqual(Path(result["backup"]).parent, real.parent)

    def test_repair_refuses_ambiguous_rules_and_unusable_settings_without_changing_anything(self) -> None:
        settings = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        code, result = self.run_helper("repair")
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertFalse(settings.parent.exists(), "a refused repair creates nothing")
        settings.parent.mkdir(parents=True)
        # A grant for the user's own program is theirs, not a retired reviewer grant: never removed.
        for ambiguous in (f"write_file({self.repo})", "command(find -delete)", "command(rg --pre=sh)", "command(git)", 7):
            with self.subTest(ambiguous=ambiguous):
                body = json.dumps({"permissions": {"allow": ["command(find)", ambiguous, "command(ls)"]}})
                settings.write_text(body)
                code, result = self.run_helper("repair")
                self.assertEqual((code, result["ok"], result["ambiguous"]), (1, False, [str(ambiguous)]))
                self.assertEqual(result["would_remove"], ["command(find)"])
                self.assertEqual(settings.read_text(), body, "an ambiguous rule stops the whole repair")
                self.assertFalse(list(settings.parent.glob("settings.json.before-*")))
        for body in ('{"permissions": {"allow": "command(find)"}}', '{"permissions": []}', "[]", "not json"):
            with self.subTest(body=body):
                settings.write_text(body)
                code, result = self.run_helper("repair")
                self.assertEqual((code, result["ok"]), (1, False))
                self.assertEqual(settings.read_text(), body)
                self.assertFalse(list(settings.parent.glob("settings.json.before-*")))
        # Nothing stale and nothing ambiguous: the file is not rewritten and no backup is made.
        body = json.dumps({"model": "gemini-test"})
        settings.write_text(body)
        code, result = self.run_helper("repair")
        self.assertEqual((code, result["removed"]), (0, []))
        self.assertEqual(settings.read_text(), body)
        self.assertFalse(list(settings.parent.glob("settings.json.before-*")))


if __name__ == "__main__":
    unittest.main()
