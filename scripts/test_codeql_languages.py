#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise CodeQL selection against real PR histories and CLI output."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import codeql_languages as selector


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        self.git("init", "--initial-branch=fixture")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.write("README.md", "fixture\n")
        self.write("native/helper.swift", "print(1)\n")
        self.write("old.py", "print(1)\n")
        self.base = self.commit()

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, capture_output=True, text=True,
        ).stdout.strip()

    def write(self, path: str, content: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def commit(self) -> str:
        self.git("add", ".")
        self.git("commit", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def event(self, head: str) -> dict:
        return {"pull_request": {"base": {"sha": self.base}, "head": {"sha": head}}}

    def selection(self, head: str) -> set[str]:
        selected, _reason = selector.select_languages("pull_request", self.event(head), self.repo)
        return selected

    def test_docs_only_cli_reports_successful_lightweight_lanes(self) -> None:
        self.write("docs/guide.md", "explanation\n")
        event_file = self.repo / "event.json"
        event_file.write_text(json.dumps(self.event(self.commit())))
        output = self.repo / "outputs.txt"
        completed = subprocess.run(
            [sys.executable, str(Path(selector.__file__)), "--event-name", "pull_request",
             "--event-path", str(event_file), "--repo", str(self.repo),
             "--github-output", str(output)],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual(json.loads(completed.stdout)["selected"], [])
        entries = json.loads(output.read_text().removeprefix("matrix="))["include"]
        self.assertEqual({entry["language"] for entry in entries}, set(selector.LANGUAGES))
        self.assertTrue(all(not entry["scan"] for entry in entries))
        self.assertTrue(all(entry["runner"].startswith("ubuntu-") for entry in entries))

    def test_swift_touch_keeps_native_extraction(self) -> None:
        self.write("native/helper.swift", "print(2)\n")
        selected = self.selection(self.commit())
        self.assertEqual(selected, {"swift"})
        entry = next(row for row in selector.matrix_for_languages(selected)["include"]
                     if row["language"] == "swift")
        self.assertTrue(entry["scan"])
        self.assertTrue(entry["runner"].startswith("macos-"))
        self.assertEqual(entry["build_mode"], "manual")

    def test_deleted_python_and_renamed_swift_are_still_scanned(self) -> None:
        self.git("mv", "native/helper.swift", "native/helper.txt")
        (self.repo / "old.py").unlink()
        self.assertEqual(self.selection(self.commit()), {"swift", "python"})

    def test_complete_diff_is_not_limited_to_api_page(self) -> None:
        for index in range(350):
            self.write(f"docs/{index}.md", "explanation\n")
        self.write("z-last.swift", "print(2)\n")
        self.assertEqual(self.selection(self.commit()), {"swift"})

    def test_new_base_work_is_not_a_pr_change(self) -> None:
        self.git("checkout", "-b", "pr")
        self.write("docs/guide.md", "explanation\n")
        head = self.commit()
        self.git("checkout", "fixture")
        self.write("base.js", "console.log(1);\n")
        self.base = self.commit()
        self.assertEqual(self.selection(head), set())

    def test_workflow_and_selector_changes_scan_every_language(self) -> None:
        for path in selector.SCAN_CONTROL_FILES:
            with self.subTest(path=path):
                self.assertEqual(selector.languages_for_paths([path]), set(selector.LANGUAGES))
        self.assertEqual(selector.languages_for_paths([".github/codeql/config.yml"]),
                         set(selector.LANGUAGES))

    def test_source_and_build_inputs_select_affected_languages(self) -> None:
        cases = {
            ".github/workflows/ci.yaml": {"actions"},
            ".github/actions/local/action.yml": {"actions"},
            "scripts/main.py": {"python"},
            "pyproject.toml": {"python"},
            "uv.lock": {"python"},
            "requirements-dev.txt": {"python"},
            "src/main.mjs": {"javascript-typescript"},
            "src/main.tsx": {"javascript-typescript"},
            "web/page.html": {"javascript-typescript"},
            "package-lock.json": {"javascript-typescript"},
            "Package.swift": {"swift"},
            "Package.resolved": {"swift"},
            "notes/Swift.md": set(),
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(selector.languages_for_paths([path]), expected)

    def test_nul_diff_preserves_unusual_filenames(self) -> None:
        self.write("nested/line\nbreak.swift", "print(2)\n")
        self.assertEqual(self.selection(self.commit()), {"swift"})

    def test_composite_action_outside_github_directory_is_scanned(self) -> None:
        self.write("skills/example/action.yml", "runs:\n  using: composite\n  steps: []\n")
        self.assertEqual(self.selection(self.commit()), {"actions"})

    def test_main_schedule_and_dispatch_scan_every_language(self) -> None:
        for event in ("push", "schedule", "workflow_dispatch"):
            with self.subTest(event=event):
                selected, _reason = selector.select_languages(event, {}, self.repo)
                self.assertEqual(selected, set(selector.LANGUAGES))
                self.assertTrue(all(row["scan"] for row in
                                    selector.matrix_for_languages(selected)["include"]))

    def test_missing_history_or_bad_event_falls_back_to_full_scan(self) -> None:
        events = ({}, self.event("0" * 40), self.event("--bad-ref"),
                  {"pull_request": {"base": {"sha": None}}})
        for event in events:
            with self.subTest(event=event):
                selected, reason = selector.select_languages("pull_request", event, self.repo)
                self.assertEqual(selected, set(selector.LANGUAGES))
                self.assertIn("unavailable", reason)


if __name__ == "__main__":
    unittest.main()
