# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import task_evidence_gc as gc


def age(path: Path, days: float) -> None:
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp), follow_symlinks=False)


class TaskEvidenceGcTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / "task-evidence"
        self.root.mkdir()
        self.config = self.base / "worktrees.json"
        self.configure()
        environment = mock.patch.dict(os.environ, DEV_WORKTREE_CONFIG=str(self.config))
        environment.start()
        self.addCleanup(environment.stop)

    def configure(self, **values: str) -> None:
        self.config.write_text(json.dumps({"evidenceRoot": str(self.root), **values}))

    def task(self, name: str = "old", days: float = 45) -> Path:
        folder = self.root / "repo" / name
        folder.mkdir(parents=True)
        age(folder, days)
        return folder

    def run_gc(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            code = gc.main(list(args))
        return code, output.getvalue()

    def test_expiry_uses_newest_change_and_preview_preserves_everything(self) -> None:
        old = self.task()
        deep = old / "nested" / "deep"
        deep.mkdir(parents=True)
        (deep / "new").write_text("recent")
        stray = self.root / "old-file"
        stray.write_text("old")
        age(stray, 45)
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        link = old / "link"
        link.symlink_to(outside)
        self.assertEqual(self.run_gc()[0], 0)
        self.assertTrue(stray.exists())
        self.assertEqual(self.run_gc("--apply")[0], 0)
        self.assertTrue((deep / "new").exists())
        self.assertFalse(stray.exists())
        for path in (deep / "new", deep, deep.parent, link, old):
            age(path, 45)
        (deep / "new").chmod(0o444)
        deep.chmod(0o555)
        self.assertEqual(self.run_gc("--apply")[0], 0)
        self.assertFalse((self.root / "repo").exists())
        self.assertTrue((outside / "keep").exists())

    def test_unreadable_or_vanishing_unit_is_kept_while_others_expire(self) -> None:
        blocked = self.task("blocked")
        (blocked / "nested").mkdir()
        age(blocked, 45)
        (blocked / "nested").chmod(0)
        self.addCleanup((blocked / "nested").chmod, 0o700)
        vanished = self.task("vanished")
        expired = self.task()
        measure = gc.newest_mtime

        def vanishing_measure(name: str, parent_fd: int) -> float:
            if name == vanished.name:
                raise FileNotFoundError("vanished")
            return measure(name, parent_fd)

        with mock.patch.object(gc, "newest_mtime", vanishing_measure):
            code, output = self.run_gc("--apply")
        self.assertEqual(code, 0)
        self.assertTrue(blocked.exists())
        self.assertTrue(vanished.exists())
        self.assertFalse(expired.exists())
        self.assertIn(str(blocked), output)
        self.assertIn(str(vanished), output)

    def test_refresh_after_expiry_rename_keeps_both_writers(self) -> None:
        old = self.task()
        rename = os.rename

        def concurrent_write(source: str, destination: str, **kwargs: int) -> None:
            rename(source, destination, **kwargs)
            old.mkdir()
            (old / "new").write_text("new writer")
            old.with_name(destination).joinpath("log").write_text("existing writer")

        with mock.patch.object(gc.os, "rename", side_effect=concurrent_write):
            self.run_gc("--apply")
        self.assertTrue((old / "new").exists())
        self.assertEqual(len(list(old.parent.glob(".expiring-*/log"))), 1)

    def test_unsafe_roots_missing_mount_and_unavailable_deletion_keep_evidence(self) -> None:
        old = self.task()
        redirect = self.base / "redirect"
        redirect.symlink_to(self.root)
        for values in ({"retainedRoot": str(self.root / "retained")}, {"root": str(self.base)},
                       {"evidenceRoot": str(redirect / "task-evidence")}, {"evidenceMount": str(self.base)}):
            with self.subTest(values=values), mock.patch("os.path.ismount", return_value=False):
                self.configure(**values)
                self.assertNotEqual(self.run_gc("--apply")[0], 0)
                self.assertTrue(old.exists())
        self.configure()
        with mock.patch.object(shutil.rmtree, "avoids_symlink_attacks", False):
            self.assertNotEqual(self.run_gc("--apply")[0], 0)
        gc.sweep(apply=True, budget_seconds=0)
        self.assertTrue(old.exists())


if __name__ == "__main__":
    unittest.main()
