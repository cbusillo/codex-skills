#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

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

    def test_unreadable_or_malformed_journal_cannot_mean_no_claims(self) -> None:
        journal.record("root", "owner/repo", 1)
        path = journal.log_path("root")
        for content in ('{"session":', '{"session":"other","repository":"owner/repo","record":1}\n', "[]\n"):
            path.write_text(content)
            with self.subTest(content=content), self.assertRaises(ValueError):
                journal.read("root")
        path.chmod(0o666)
        with self.assertRaises(ValueError):
            journal.read("root")

    def test_symlinked_store_or_file_refuses_read_and_write(self) -> None:
        journal.record("root", "owner/repo", 1)
        path = journal.log_path("root")
        saved = path.with_suffix(".saved")
        path.rename(saved)
        path.symlink_to(saved)
        for operation in (lambda: journal.read("root"), lambda: journal.record("root", "owner/repo", 2)):
            with self.assertRaises((OSError, ValueError)):
                operation()
        path.unlink()
        saved.unlink()
        directory = path.parent
        directory.rmdir()
        directory.symlink_to(self.folder, target_is_directory=True)
        with self.assertRaises(ValueError):
            journal.read("root")


if __name__ == "__main__":
    unittest.main()
