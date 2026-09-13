#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline regression checks for the retired, inert Every Code entry point."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("local_code_agent.py")


class RetiredEntryPointTests(unittest.TestCase):
    def test_main_does_not_read_prompt_config_or_resolve_a_binary(self) -> None:
        spec = importlib.util.spec_from_file_location("retired_local_code_agent", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with (
            patch("builtins.open", side_effect=AssertionError("unexpected file read")),
            patch("sys.stdin", None),
            patch("subprocess.Popen", side_effect=AssertionError("unexpected process")),
            patch("pathlib.Path.mkdir", side_effect=AssertionError("unexpected directory")),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
        ):
            self.assertEqual(module.main(), 2)
        self.assertIn("retired", stderr.getvalue())
        self.assertIn("local_codex_agent.py", stderr.getvalue())

    def test_all_old_arguments_fail_without_consuming_open_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "must-not-exist"
            fake_code = root / "code"
            marker = root / "executed"
            fake_code.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
            fake_code.chmod(0o700)
            for arguments in (
                [], ["--help"],
                ["--code-bin", str(fake_code), "--keep-code-home", str(home), "--config", str(root / "missing.yaml"), "-"],
            ):
                with self.subTest(arguments=arguments):
                    with subprocess.Popen(
                        [sys.executable, "-S", str(SCRIPT), *arguments],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    ) as process:
                        # Keep stdin open: reading it would block the retired CLI.
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                            self.fail("retired helper tried to read stdin or execute work")
                        stdout, stderr = process.communicate()
                    self.assertEqual(process.returncode, 2)
                    self.assertFalse(stdout)
                    self.assertIn(b"retired", stderr)
                    self.assertFalse(home.exists())
                    self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
