#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Account move behavior with disposable homes and fake Context Panel snapshots."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import claude_account as account


class MoveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.account_reader = account.reader()
        self.reader_patch = patch.object(account, "reader", return_value=self.account_reader)
        self.reader_patch.start()
        self.addCleanup(self.reader_patch.stop)
        self.runtime_patch = patch.object(account, "runtime_script", return_value=Path(account.__file__).resolve())
        self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)
        self.old = self.root / "old"
        self.new = self.root / "new"
        self.old.mkdir()
        self.new.mkdir()
        (self.old / "settings.json").write_text("{}")
        (self.new / "settings.json").symlink_to(self.old / "settings.json")
        self.projects = self.old / "projects"
        self.projects.mkdir()
        (self.new / "projects").symlink_to(self.projects, target_is_directory=True)
        self.identifier = str(uuid.uuid4())
        self.transcript = self.projects / (self.identifier + ".jsonl")
        self.transcript.write_text('{"context":"prior work"}\n')
        self.env = {"CLAUDE_CONFIG_DIR": str(self.old), account.MOVE_DIR: str(self.root / "moves"),
                    "INHERITED": "keep me", account.ENABLED: "1", "CLAUDE_CODE_PROCESS_WRAPPER": "keep wrapper"}
        self.payload = {"hook_event_name": "StopFailure", "error": "rate_limit",
                        "session_id": self.identifier, "cwd": str(Path.cwd()),
                        "transcript_path": str(self.transcript)}
        self.request = self.root / "moves" / (self.identifier + ".json")
        self.argv = ["/fake/claude", "--resume", self.identifier, "--model", "unchanged value"]
        self.choice = {"source": "context-panel", "name": "next", "env": {"CLAUDE_CONFIG_DIR": str(self.new)}}

    def record(self):
        account.hook(self.payload, self.env)

    def move(self, choice=None):
        with patch.object(self.account_reader, "load_config", return_value={"snapshot_command": ["fake"]}), \
             patch.object(self.account_reader, "read_snapshot", return_value=({}, None)), \
             patch.object(self.account_reader, "choose", return_value=choice or self.choice):
            return account.move_environment(self.argv, self.env)

    def test_disabled_and_unrelated_hooks_are_inert(self):
        account.hook(self.payload, {})
        account.hook({**self.payload, "error": "overloaded"}, self.env)
        account.hook({**self.payload, "session_id": "../../escape"}, self.env)
        self.assertFalse(self.request.parent.exists())

    def test_rate_limit_records_only_affected_session_and_no_error_text(self):
        account.hook({**self.payload, "error_details": "private data"}, self.env)
        data = account.read_request(self.request)
        self.assertEqual(data["session_id"], self.identifier)
        self.assertNotIn("private data", self.request.read_text())
        self.assertEqual(self.transcript.read_text(), '{"context":"prior work"}\n')

    def test_ordinary_and_other_session_launches_never_query_chooser(self):
        self.record()
        with patch.object(self.account_reader, "load_config", side_effect=AssertionError("must not read")):
            for argv in (["claude", "--print", "hello"], ["claude", "--resume", str(uuid.uuid4())],
                         ["claude", "--", "--resume", self.identifier]):
                self.assertEqual(account.move_environment(argv, self.env), self.env)

    def test_move_changes_only_config_home_preserves_request_until_confirmed(self):
        self.record()
        moved = self.move()
        self.assertEqual(moved, {**self.env, "CLAUDE_CONFIG_DIR": str(self.new)})
        self.assertTrue(self.request.exists())
        for home, source, identifier in ((self.old, "resume", self.identifier),
                                         (self.new, "startup", self.identifier),
                                         (self.new, "resume", str(uuid.uuid4()))):
            account.hook({**self.payload, "hook_event_name": "SessionStart", "source": source,
                          "session_id": identifier}, {**self.env, "CLAUDE_CONFIG_DIR": str(home)})
            self.assertTrue(self.request.exists())
        account.hook({**self.payload, "hook_event_name": "SessionStart", "source": "resume"}, moved)
        self.assertFalse(self.request.exists())
        receipt = json.loads(self.request.with_name(self.identifier + ".resumed.json").read_text())
        self.assertEqual(receipt["session_id"], self.identifier)
        self.assertEqual(receipt["state"], "resumed")

    def test_pending_attempt_retries_same_target_without_new_choice(self):
        self.record()
        self.move()
        with patch.object(self.account_reader, "load_config", side_effect=AssertionError("must reuse")):
            self.assertEqual(account.move_environment(self.argv, self.env)["CLAUDE_CONFIG_DIR"], str(self.new))

    def test_choice_failure_and_same_account_preserve_recovery(self):
        self.record()
        with patch.object(self.account_reader, "load_config", side_effect=ValueError("snapshot unavailable")):
            self.assertEqual(account.move_environment(self.argv, self.env), self.env)
        self.assertTrue(self.request.exists())
        self.assertEqual(self.move({**self.choice, "env": {"CLAUDE_CONFIG_DIR": str(self.old)}}), self.env)
        self.assertEqual(self.move({**self.choice, "source": "context-panel-stale"}), self.env)

    def test_unshared_history_and_wrong_cwd_refuse_move(self):
        self.record()
        (self.new / "projects").unlink()
        (self.new / "projects").mkdir()
        self.assertEqual(self.move(), self.env)
        with patch.object(account.Path, "cwd", return_value=self.root):
            self.assertEqual(self.move(), self.env)
        self.assertTrue(self.request.exists())

    def test_resume_forms_and_duplicate_or_path_resume(self):
        self.assertEqual(account.resumed_session(["claude", "--resume=" + self.identifier]), self.identifier)
        self.assertEqual(account.resumed_session(["claude", "-r", self.identifier]), self.identifier)
        self.assertIsNone(account.resumed_session(["claude", "--resume", str(self.transcript)]))
        self.assertIsNone(account.resumed_session([*self.argv, "--resume", self.identifier]))

    def test_exec_failure_keeps_move_request(self):
        self.record()
        self.move()
        with patch.dict(os.environ, self.env, clear=True), patch.object(account.os, "execvpe", side_effect=OSError):
            self.assertEqual(account.wrapper(self.argv), 127)
        self.assertTrue(self.request.exists())

    def test_real_wrapper_exec_preserves_arguments_environment_and_exit_status(self):
        fake = self.root / "fake.py"
        fake.write_text("import os,sys,json\nprint(json.dumps({'args':sys.argv[1:], 'keep':os.environ['INHERITED'], 'home':os.environ['CLAUDE_CONFIG_DIR']}))\nsys.exit(17)\n")
        result = subprocess.run([sys.executable, str(Path(account.__file__)), "wrap", sys.executable,
                                 str(fake), "space value", "--resume=unused"],
                                env={**os.environ, **self.env}, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 17)
        self.assertEqual(json.loads(result.stdout), {"args": ["space value", "--resume=unused"],
                                                   "keep": "keep me", "home": str(self.old)})
        self.assertEqual(result.stderr, "")

    def test_snapshot_timeout_is_short_and_unavailable_is_preserved(self):
        with patch.object(account.subprocess, "run", side_effect=subprocess.TimeoutExpired("fake", 1.5)) as run:
            snapshot, problem = self.account_reader.read_snapshot(["fake"], runner=account.bounded_snapshot)
        self.assertIsNone(snapshot)
        self.assertIn("TimeoutExpired", problem)
        self.assertLess(run.call_args.kwargs["timeout"], 3)

    def test_cross_home_exec_resumes_same_transcript_through_real_reader(self):
        self.record()
        private = self.root / "code" / "skill-data"
        private.mkdir(parents=True)
        snapshot = self.root / "snapshot.py"
        snapshot.write_text("import json\nprint(json.dumps(" + repr({
            "schemaVersion": 1,
            "accounts": [{"provider": "anthropic", "id": "anthropic-b", "label": "next"}],
            "answers": {"useNext": [{"provider": "anthropic", "accountID": "anthropic-b"}]}
        }) + "))\n")
        (private / "supervisor.toml").write_text(
            "[accounts]\nsnapshot_command = " + json.dumps([sys.executable, str(snapshot)]) + "\n"
            "[[accounts.account]]\nname = \"next\"\nprovider = \"anthropic\"\ncontext_panel_label = \"next\"\n"
            "[accounts.account.env]\nCLAUDE_CONFIG_DIR = " + json.dumps(str(self.new)) + "\n")
        child = self.root / "resume.py"
        child.write_text("import json,os,sys\nfrom pathlib import Path\n"
                         "identifier=sys.argv[sys.argv.index('--resume')+1]\n"
                         "transcript=Path(os.environ['CLAUDE_CONFIG_DIR'])/'projects'/(identifier+'.jsonl')\n"
                         "print(json.dumps({'session':identifier,'context':json.loads(transcript.read_text())['context'],"
                         "'home':os.environ['CLAUDE_CONFIG_DIR'],'keep':os.environ['INHERITED']}))\n")
        env = {**os.environ, **self.env, "CODE_HOME": str(private.parent), "HOME": str(self.root)}
        for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN",
                    "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
            env.pop(key, None)
        result = subprocess.run([sys.executable, account.__file__, "wrap", sys.executable,
                                 str(child), "--resume", self.identifier], env=env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"session": self.identifier, "context": "prior work",
                                                   "home": str(self.new), "keep": "keep me"})
        self.assertTrue(self.request.exists())

    def test_authentication_override_does_not_move_or_expose_value(self):
        self.record()
        env = {**self.env, "ANTHROPIC_API_KEY": "test-private-value"}
        self.assertEqual(account.move_environment(self.argv, env), env)
        self.assertNotIn("test-private-value", self.request.read_text())

    def test_shared_settings_director_session_is_inert(self):
        env = {k: v for k, v in self.env.items() if k != account.ENABLED}
        account.hook(self.payload, env)
        self.assertFalse(self.request.exists())
        self.record()
        self.assertEqual(account.move_environment(self.argv, env), env)

    def test_installed_wrapper_preserves_every_environment_variable(self):
        settings = self.root / "settings.json"
        installed = account.install(settings, self.root / "moves")
        argv = json.loads(installed["env"]["CLAUDE_CODE_PROCESS_WRAPPER"])
        code = "import os,json;print(json.dumps(dict(os.environ)))"
        env = {**os.environ, **self.env, "HOME": str(self.root), "PYTHONPATH": "unchanged"}
        direct = subprocess.check_output([sys.executable, "-I", "-c", code], env=env)
        wrapped = subprocess.check_output([*argv, sys.executable, "-I", "-c", code], env=env)
        self.assertEqual(json.loads(wrapped), json.loads(direct))

    def test_enrollment_refuses_task_worktree(self):
        self.runtime_patch.stop()
        from types import SimpleNamespace
        module = SimpleNamespace(runtime_skills_paths=lambda: [(Path(account.__file__).resolve().parents[2], "fixture", True)])
        responses = [SimpleNamespace(returncode=0, stdout="work/task"),
                     SimpleNamespace(returncode=0, stdout="origin/main")]
        with patch.object(account, "load_module", return_value=module), \
             patch.object(account.subprocess, "run", side_effect=responses):
            with self.assertRaises(ValueError):
                account.runtime_script()

    def test_settings_and_auth_modes_cannot_acknowledge_wrong_account(self):
        self.record()
        (self.new / "settings.json").unlink()
        (self.new / "settings.json").write_text("{}")
        self.assertEqual(self.move(), self.env)
        (self.new / "settings.json").unlink()
        (self.new / "settings.json").symlink_to(self.old / "settings.json")
        (self.old / "settings.json").write_text('{"apiKeyHelper":"fixture-command"}')
        self.assertEqual(self.move(), self.env)
        for key in ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
            env = {**self.env, key: "fixture-override"}
            self.assertEqual(account.move_environment(self.argv, env), env)

    def test_cancel_removes_only_requested_move_without_touching_transcript(self):
        self.record()
        other = str(uuid.uuid4())
        account.hook({**self.payload, "session_id": other,
                      "transcript_path": str(self.projects / (other + ".jsonl"))}, self.env)
        other_path = self.request.parent / (other + ".json")
        other_path.write_text(json.dumps({"schema": 1, "session_id": other}))
        result = subprocess.run([sys.executable, account.__file__, "cancel", "--session-id", self.identifier],
                                env={**os.environ, **self.env}, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.request.exists())
        self.assertTrue(other_path.exists())
        self.assertTrue(self.transcript.exists())

    def test_status_without_enrollment_and_unsafe_directory_are_visible(self):
        env = {k: v for k, v in os.environ.items() if k != account.MOVE_DIR}
        result = subprocess.run([sys.executable, account.__file__, "status"], env=env,
                                capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        directory = self.root / "moves"
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
        with self.assertRaises(ValueError):
            account.install(self.root / "settings.json", directory, write=True)
        self.assertFalse((self.root / "settings.json").exists())

    def test_installer_preview_preserves_settings_and_repeat_is_idempotent(self):
        settings = self.root / "settings.json"
        existing = {"permissions": {"defaultMode": "default"}, "env": {"OTHER": "yes"},
                    "hooks": {"StopFailure": [{"matcher": "overloaded", "hooks": []}]}}
        settings.write_text(json.dumps(existing))
        preview = account.install(settings, self.root / "moves")
        self.assertEqual(json.loads(settings.read_text()), existing)
        self.assertEqual(preview["permissions"], existing["permissions"])
        account.install(settings, self.root / "moves", write=True)
        before = settings.read_bytes()
        account.install(settings, self.root / "moves", write=True)
        self.assertEqual(settings.read_bytes(), before)
        data = json.loads(before)
        self.assertEqual(data["env"]["OTHER"], "yes")
        self.assertIn(existing["hooks"]["StopFailure"][0], data["hooks"]["StopFailure"])

    def test_installer_conflicting_wrapper_and_symlink_are_preserved(self):
        settings = self.root / "settings.json"
        settings.write_text('{"env":{"CLAUDE_CODE_PROCESS_WRAPPER":"other"}}')
        before = settings.read_bytes()
        with self.assertRaises(ValueError):
            account.install(settings, self.root / "moves", write=True)
        self.assertEqual(settings.read_bytes(), before)
        link = self.root / "link.json"
        link.symlink_to(settings)
        with self.assertRaises(ValueError):
            account.install(link, self.root / "moves", write=True)
        self.assertEqual(settings.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
