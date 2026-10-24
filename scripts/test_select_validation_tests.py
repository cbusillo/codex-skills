#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import select_validation_tests as selector

HELPERS = [
    "scripts/validate_words.py",
    "skills/a/scripts/test_hook.py",
    "skills/a/scripts/test_tool.py",
    "skills/b/scripts/test_other.py",
]


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.write(".github/CODEOWNERS", "/DIRECTION.md @owner\n")
        self.write("DIRECTION.md", "direction\n")
        self.write("scripts/validate_words.py", "print('scan')\n")
        self.write("skills/a/scripts/shared.py", "VALUE = 1\n")
        self.write("skills/a/scripts/tool.py", "import shared\n")
        self.write("skills/a/scripts/test_tool.py", "TOOL = 'tool.py'\n")
        self.write("skills/a/scripts/run-me.py", "print('run')\n")
        self.write("skills/a/scripts/hook.py", "HELPER = load_sibling('run-me.py')\nprint('see notes.py')\n")
        self.write("skills/a/scripts/test_hook.py", "import hook\n")
        self.write("skills/a/scripts/notes.py", "print('notes')\n")
        self.write("skills/a/SKILL.md", "skill\n")
        self.write("skills/b/scripts/test_other.py", "print('other')\n")
        self.commit()
        self.git("checkout", "-b", "work/change")

    def git(self, *args: str) -> str:
        environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        return subprocess.run(
            ["git", *args], cwd=self.repo, env=environment,
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def write(self, path: str, content: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def commit(self) -> str:
        self.git("add", "-A")
        self.git("commit", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def pull_request_checkout(self, head_ref: str = "work/change") -> dict:
        head = self.commit()
        self.git("checkout", "--detach", "main")
        self.git("merge", "--no-ff", "-m", "merge", head)
        return {"pull_request": {"head": {"ref": head_ref, "sha": head}}}

    def selection(self, event: dict, event_name: str = "pull_request") -> selector.Selection:
        return selector.select_helpers(event_name, event, self.repo, HELPERS)

    def test_merge_train_candidate_runs_every_helper(self) -> None:
        self.write("skills/b/scripts/test_other.py", "print('changed')\n")
        event = self.pull_request_checkout("launchplane/train/cbusillo/codex-skills/main/batch")
        self.assertIsNone(self.selection(event).helpers)

    def test_push_schedule_and_manual_runs_run_every_helper(self) -> None:
        self.write("skills/b/scripts/test_other.py", "print('changed')\n")
        event = self.pull_request_checkout()
        for event_name in ("push", "workflow_dispatch", "schedule", ""):
            with self.subTest(event_name=event_name):
                self.assertIsNone(self.selection(event, event_name).helpers)

    def test_checkout_that_is_not_the_pull_request_merge_runs_every_helper(self) -> None:
        self.write("skills/b/scripts/test_other.py", "print('changed')\n")
        event = self.pull_request_checkout()
        event["pull_request"]["head"]["sha"] = self.git("rev-parse", "main")
        self.assertIsNone(self.selection(event).helpers)

    def test_malformed_event_runs_every_helper(self) -> None:
        self.write("skills/b/scripts/test_other.py", "print('changed')\n")
        self.pull_request_checkout()
        for event in ({}, {"pull_request": {"head": {"ref": "work/change"}}}):
            with self.subTest(event=event):
                self.assertIsNone(self.selection(event).helpers)

    def test_code_owned_change_that_can_land_outside_the_train_runs_every_helper(self) -> None:
        self.write("DIRECTION.md", "new direction\n")
        self.assertIsNone(self.selection(self.pull_request_checkout()).helpers)

    def test_validation_control_change_runs_every_helper(self) -> None:
        for path in (
            ".github/workflows/validate.yml", "scripts/validate-skills.sh", "scripts/run-python-helper.sh",
            "scripts/select_validation_tests.py", "uv.toml", ".python-version",
        ):
            with self.subTest(path=path):
                self.assertTrue(selector.requires_every_helper(path, []))

    def test_change_selects_helpers_that_reach_it_through_other_modules(self) -> None:
        self.write("skills/a/scripts/shared.py", "VALUE = 2\n")
        selected = self.selection(self.pull_request_checkout()).helpers
        self.assertEqual(selected, ["scripts/validate_words.py", "skills/a/scripts/test_tool.py"])

    def test_script_a_module_loads_by_path_selects_the_module_tests(self) -> None:
        self.write("skills/a/scripts/run-me.py", "print('changed')\n")
        selected = self.selection(self.pull_request_checkout()).helpers
        self.assertEqual(selected, ["scripts/validate_words.py", "skills/a/scripts/test_hook.py"])

    def test_file_a_module_only_names_in_a_message_selects_no_module_tests(self) -> None:
        self.write("skills/a/scripts/notes.py", "print('changed')\n")
        self.assertEqual(self.selection(self.pull_request_checkout()).helpers, ["scripts/validate_words.py"])

    def test_deleted_module_selects_its_former_callers(self) -> None:
        (self.repo / "skills/a/scripts/tool.py").unlink()
        selected = self.selection(self.pull_request_checkout()).helpers
        self.assertEqual(selected, ["scripts/validate_words.py", "skills/a/scripts/test_tool.py"])

    def test_unreferenced_document_runs_only_repository_scanners(self) -> None:
        self.write("skills/a/SKILL.md", "changed skill\n")
        selected = self.selection(self.pull_request_checkout()).helpers
        self.assertEqual(selected, ["scripts/validate_words.py"])

    def test_cli_exits_for_the_full_suite_outside_pull_requests(self) -> None:
        self.write("skills/b/scripts/test_other.py", "print('changed')\n")
        event_file = self.repo.parent / f"{self.repo.name}-event.json"
        self.addCleanup(event_file.unlink, missing_ok=True)
        event_file.write_text(json.dumps(self.pull_request_checkout()))
        outputs = {}
        for event_name in ("pull_request", "push"):
            outputs[event_name] = subprocess.run(
                [sys.executable, str(Path(selector.__file__))],
                cwd=self.repo, input="\n".join(HELPERS), capture_output=True, text=True,
                env={**os.environ, "GITHUB_EVENT_NAME": event_name, "GITHUB_EVENT_PATH": str(event_file)},
            )
        self.assertEqual(outputs["pull_request"].returncode, 0, outputs["pull_request"].stderr)
        self.assertEqual(outputs["pull_request"].stdout.split(),
                         ["scripts/validate_words.py", "skills/b/scripts/test_other.py"])
        self.assertEqual(outputs["push"].returncode, selector.FULL_SUITE_EXIT, outputs["push"].stderr)
        self.assertEqual(outputs["push"].stdout, "")


if __name__ == "__main__":
    unittest.main()
