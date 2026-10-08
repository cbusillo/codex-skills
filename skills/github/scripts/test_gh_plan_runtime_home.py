#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline planning-config routing tests with isolated home fixtures."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("gh-plan.py")
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("gh_plan_runtime_home_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PLAN: Any = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)


class RuntimeHomeTests(unittest.TestCase):
    def test_planning_config_uses_runtime_binding_and_ignores_legacy_plans(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            code, codex = home / ".code", home / ".codex"
            codex.mkdir()
            (codex / "github-planning.json").write_text("codex config")
            with patch.dict(os.environ, {"HOME": str(home)}, clear=True):
                self.assertEqual(PLAN.workspace_config_path().read_text(), "codex config")
                (code / "plans").mkdir(parents=True)
                (code / "github-planning.json").write_text("code config")
                self.assertEqual(PLAN.workspace_config_path().read_text(), "codex config")
                (code / "skills").mkdir()
                self.assertEqual(PLAN.workspace_config_path().read_text(), "code config")

    def test_explicit_runtime_homes_keep_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            code, codex = home / "code-runtime", home / "codex-runtime"
            with patch.dict(os.environ, {"HOME": str(home), "CODE_HOME": str(code),
                                         "CODEX_HOME": str(codex)}, clear=True):
                self.assertEqual(PLAN.workspace_config_path(), code / "github-planning.json")
                del os.environ["CODE_HOME"]
                self.assertEqual(PLAN.workspace_config_path(), codex / "github-planning.json")


if __name__ == "__main__":
    unittest.main()
