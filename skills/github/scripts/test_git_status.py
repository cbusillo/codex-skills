#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from git_status import read_status


class GitStatusTests(unittest.TestCase):
    def test_source_and_retirement_use_git_ignores_without_hiding_tracked_changes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def git(*args: str) -> bytes:
                return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL)

            git("init", "-q")
            (root / ".gitignore").write_text("generated/\n")
            generated = root / "generated"
            generated.mkdir()
            (generated / "tracked").write_text("original")
            git("add", ".gitignore")
            git("add", "-f", "generated/tracked")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "initial")
            (generated / "cache").write_text("generated")
            self.assertFalse(read_status(git))
            self.assertTrue(read_status(git, ignored=True))
            unusual = " leading space\nrésumé"
            git("mv", "generated/tracked", unusual)
            changes = read_status(git)
            self.assertEqual([path for change in changes for path in change.paths], [unusual, "generated/tracked"])
            (root / "new source").write_text("keep")
            self.assertEqual(len(read_status(git)), 2)
            self.assertEqual(read_status(git, untracked=False), changes)

    def test_review_scratch_exemption_never_hides_tracked_or_nested_files(self) -> None:
        raw = b"?? .model-review-one/diff\0 M .model-review-two/source\0?? nested/.model-review-three/file\0"
        changes = read_status(lambda *args: raw, allow_review_scratch=True)
        self.assertEqual([change.paths[0] for change in changes], [".model-review-two/source", "nested/.model-review-three/file"])
        self.assertEqual(len(read_status(lambda *args: raw)), 3)

    def test_status_preserves_copy_sources_and_rejects_truncated_records(self) -> None:
        self.assertEqual(read_status(lambda *args: b"C  destination\0original\0")[0].paths, ("destination", "original"))
        self.assertEqual(read_status(lambda *args: b"?? name-\xff\0")[0].paths, (os.fsdecode(b"name-\xff"),))
        for raw in (b"?? missing terminator", b"R  destination\0", b"R  destination\0original", b"garbled\0"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                read_status(lambda *args, raw=raw: raw)


if __name__ == "__main__":
    unittest.main()
