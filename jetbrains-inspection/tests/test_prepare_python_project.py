#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import importlib.util
import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "prepare-python-project.py"
SPEC = importlib.util.spec_from_file_location("prepare_python_project", SCRIPT_PATH)
prepare_python_project = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(prepare_python_project)


class PreparePythonProjectTest(unittest.TestCase):
    def test_sdk_name_uses_home_relative_form(self):
        self.assertEqual(
            prepare_python_project.sdk_name_for(Path("/Users/example/work/.venv"), Path("/Users/example")),
            "~/work/.venv",
        )

    def test_rendered_project_model_uses_sdk_and_test_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            with patch.object(prepare_python_project.Path, "home", return_value=repo.parent):
                files = prepare_python_project.render_project_files(repo, "codex-skills", ["jetbrains-inspection/tests"])

        module = files[repo / ".idea" / "codex-skills.iml"]
        self.assertIn('content url="file://$MODULE_DIR$"', module)
        self.assertIn('isTestSource="true"', module)
        self.assertIn('jdkType="Python SDK"', module)
        self.assertIn("jetbrains-inspection/tests", module)
        self.assertIn("codex-skills.iml", files[repo / ".idea" / "modules.xml"])
        self.assertIn("project-jdk-name=", files[repo / ".idea" / "misc.xml"])

    def test_atomic_write_skips_unchanged_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "file.xml"
            prepare_python_project.atomic_write(path, "same\n")
            before = path.stat().st_mtime_ns
            prepare_python_project.atomic_write(path, "same\n")
            self.assertEqual(path.stat().st_mtime_ns, before)

    def test_preflight_requires_ignored_untracked_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / ".gitignore").write_text(".venv/\n.idea/misc.xml\n", encoding="utf-8")
            prepare_python_project.ensure_ignored_outputs(
                repo,
                [repo / ".venv", repo / ".idea" / "misc.xml"],
            )

            (repo / ".idea").mkdir()
            (repo / ".idea" / "misc.xml").write_text("tracked\n", encoding="utf-8")
            subprocess.run(["git", "add", "-f", ".idea/misc.xml"], cwd=repo, check=True)
            with self.assertRaisesRegex(RuntimeError, "must not be tracked"):
                prepare_python_project.ensure_ignored_outputs(repo, [repo / ".idea" / "misc.xml"])


if __name__ == "__main__":
    unittest.main()
