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

    def test_each_attempt_records_its_own_start_and_end(self) -> None:
        with journal.attempt("root", "owner/repo", 1):
            with journal.attempt("root", "owner/repo", 1):
                self.assertEqual([a.running for a in journal.attempts("root")], [True, True])
            self.assertEqual(sorted(a.running for a in journal.attempts("root")), [False, True])
        self.assertEqual({a.target for a in journal.attempts("root") if not a.running}, {("owner/repo", 1)})
        self.assertEqual(journal.read("root"), {("owner/repo", 1)})

    def test_attempt_that_raises_still_records_its_end(self) -> None:
        with self.assertRaises(OSError), journal.attempt("root", "owner/repo", 1):
            raise OSError("post failed")
        self.assertFalse(journal.attempts("root")[0].running)

    def test_attempt_is_visible_before_its_target_is_journaled(self) -> None:
        with patch.object(journal, "record", side_effect=OSError("journal unavailable")):
            with self.assertRaises(OSError), journal.attempt("root", "owner/repo", 1):
                pass
        self.assertEqual([a.target for a in journal.attempts("root")], [("owner/repo", 1)])
        self.assertEqual(journal.read("root"), set())

    def test_malformed_attempt_cannot_mean_no_running_claim(self) -> None:
        with journal.attempt("root", "owner/repo", 1):
            path = next((journal.log_path("root") / "attempts").glob("*.json"))
        valid = '"session":"root","repository":"owner/repo","record":1,"ended":true'
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
        path.unlink()
        saved.unlink()
        directory = path.parent
        directory.rmdir()
        directory.symlink_to(self.folder, target_is_directory=True)
        with self.assertRaises(ValueError):
            journal.read("root")


if __name__ == "__main__":
    unittest.main()
