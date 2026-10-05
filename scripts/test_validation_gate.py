#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise the gate's shell preflight without running the catalog suite."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


GATE = Path(__file__).with_name("validate-skills.sh")


def supports_mapfile(shell: str) -> bool:
    return subprocess.run(
        [shell, "-c", "builtin mapfile -t lines < /dev/null"],
        capture_output=True, check=False,
    ).returncode == 0


class ValidationGateTests(unittest.TestCase):
    def test_unsupported_shell_refuses_before_external_commands(self) -> None:
        shell = "/bin/bash"
        if not Path(shell).is_file() or supports_mapfile(shell):
            self.skipTest("host has no unsupported system Bash")
        result = subprocess.run(
            [shell, str(GATE)], env={**os.environ, "PATH": ""},
            capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Bash", result.stderr)
        self.assertIn("execution-environment.md#", result.stderr)
        self.assertNotIn("command not found", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_supported_shell_reaches_runtime_and_reports_itself(self) -> None:
        shell = shutil.which("bash")
        if not shell or not supports_mapfile(shell):
            self.skipTest("host has no supported Bash on PATH")
        version = subprocess.check_output(
            [shell, "-c", 'printf "%s" "$BASH_VERSION"'], text=True,
        )
        with tempfile.TemporaryDirectory() as temporary:
            bin_dir = Path(temporary)
            for command in ("bash", "git", "gh", "jq", "node", "uv"):
                stub = bin_dir / command
                stub.write_text(
                    '#!/bin/sh\n'
                    'if [ "$1" = run ]; then\n'
                    '  echo runtime-reached >&2\n'
                    '  exit 42\n'
                    'fi\n'
                    'echo stub-version\n', encoding="utf-8",
                )
                stub.chmod(0o755)
            result = subprocess.run(
                [shell, str(GATE)],
                env={**os.environ, "PATH": f"{bin_dir}:{os.defpath}"},
                capture_output=True, text=True, check=False,
            )
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn("runtime-reached", result.stderr)
        self.assertIn(version, result.stdout)


if __name__ == "__main__":
    unittest.main()
