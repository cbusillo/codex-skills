#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import github_plan_claim_log as journal


class ClaimLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(Path, "home", return_value=self.folder))

    def test_root_journal_covers_concurrent_child_claims_across_repositories(self) -> None:
        records = [(f"owner/repo-{number}", number) for number in range(1, 17)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda row: journal.record("root", *row), records * 2))
        self.assertEqual(journal.read("root"), set(records))
        self.assertEqual(journal.read("another-session"), set())

    def test_interrupted_write_leaves_the_previous_targets_readable(self) -> None:
        journal.record("root", "owner/repo", 1)
        with patch.object(journal.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                journal.record("root", "owner/other", 2)
        self.assertEqual(journal.read("root"), {("owner/repo", 1)})

    def test_unreadable_or_malformed_journal_cannot_mean_no_claims(self) -> None:
        journal.record("root", "owner/repo", 1)
        path = next(journal.log_path("root").glob("*.json"))
        for content in ('{"session":', '{"session":"other","repository":"owner/repo","record":1}\n', "[]\n"):
            path.write_text(content)
            with self.subTest(content=content), self.assertRaises(ValueError):
                journal.read("root")
        path.chmod(0o666)
        with self.assertRaises(ValueError):
            journal.read("root")

    def test_each_attempt_is_recorded_before_its_target(self) -> None:
        journal.record("root", "owner/repo", 1)
        journal.record("root", "owner/repo", 1)
        attempts = journal.attempts("root")
        self.assertEqual([(a.target, a.pid, a.group) for a in attempts], [(("owner/repo", 1), os.getpid(), os.getpgid(0))] * 2)
        self.assertEqual(len({a.identifier for a in attempts}), 2)
        with patch.object(journal, "write_row", side_effect=[None, OSError("disk full")]), self.assertRaises(OSError):
            journal.record("root", "owner/other", 2)
        self.assertEqual(journal.read("root"), {("owner/repo", 1)})

    def test_attempt_runs_while_its_process_or_group_is_alive(self) -> None:
        journal.record("root", "owner/repo", 1)
        self.assertTrue(journal.attempts("root")[0].running)
        with patch.object(journal.os, "kill", side_effect=ProcessLookupError), patch.object(journal.os, "killpg", side_effect=ProcessLookupError):
            self.assertFalse(journal.attempts("root")[0].running)
        with patch.object(journal.os, "kill", side_effect=ProcessLookupError), patch.object(journal.os, "killpg", side_effect=PermissionError):
            self.assertTrue(journal.attempts("root")[0].running)

    def test_malformed_attempt_cannot_mean_no_running_claim(self) -> None:
        journal.record("root", "owner/repo", 1)
        path = next(journal.attempts_path("root").glob("*.json"))
        valid = '"session":"root","repository":"owner/repo","record":1'
        for content in ('{"pid":', f'{{{valid},"pid":"1","group":1}}', f'{{{valid},"pid":1}}'):
            path.write_text(content)
            with self.subTest(content=content), self.assertRaises(ValueError):
                journal.attempts("root")

    def test_symlinked_store_or_file_refuses_read_and_write(self) -> None:
        journal.record("root", "owner/repo", 1)
        path = next(journal.log_path("root").glob("*.json"))
        saved = path.with_suffix(".saved")
        path.rename(saved)
        path.symlink_to(saved)
        original = saved.read_bytes()
        with self.assertRaises((OSError, ValueError)):
            journal.read("root")
        journal.record("root", "owner/repo", 1)
        self.assertFalse(path.is_symlink())
        self.assertEqual(saved.read_bytes(), original)
        directory = path.parent
        shutil.rmtree(directory)
        directory.symlink_to(self.folder, target_is_directory=True)
        with self.assertRaises(ValueError):
            journal.read("root")
        with self.assertRaises(ValueError):
            journal.attempts("root")


if __name__ == "__main__":
    unittest.main()
