#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

import contextlib
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import stranded_archive as digest

FULL = "https://github.com/example/catalog/issues/4#issuecomment-999"


class ArchiveTests(unittest.TestCase):
    def test_archive_uses_existing_bot_transport_and_deduplication(self) -> None:
        comment = mock.Mock(return_value={"url": FULL})
        module = types.SimpleNamespace(comment=comment, CommentError=RuntimeError)
        with mock.patch.dict(sys.modules, {"github_comment": module}):
            self.assertEqual(digest.archive("full list", "example/catalog#4"), FULL)
            self.assertEqual(digest.archive("full list", "example/catalog#4"), FULL)
        comment.assert_called_with("issue", 4, "full list", repo="example/catalog", dedupe_body=True)

    def test_archive_failure_returns_no_delivery_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "report.md"
            report.write_text("retained report")
            comment = mock.Mock()
            module = types.SimpleNamespace(comment=comment, CommentError=RuntimeError)
            outcomes = [{}, {"url": "https://github.com/other/catalog/issues/4#issuecomment-999"},
                        RuntimeError("private transport failure")]
            for outcome in outcomes:
                output, error = io.StringIO(), io.StringIO()
                comment.side_effect = outcome if isinstance(outcome, Exception) else None
                comment.return_value = outcome
                with self.subTest(outcome=outcome), mock.patch.dict(sys.modules, {"github_comment": module}), \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                    self.assertEqual(digest.main(["--file", str(report), "--tracking-issue", "example/catalog#4"]), 1)
                self.assertFalse(output.getvalue())
                self.assertTrue(error.getvalue())
                self.assertNotIn("private transport failure", error.getvalue())


if __name__ == "__main__":
    unittest.main()
