#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from test_jb_inspect import jb_inspect


class NestedPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "scripts"
        self.project.mkdir()
        (self.root / ".idea").mkdir()
        self.jvm_model = self.root / ".idea/misc.xml"
        self.jvm_model.write_text('<project><component name="ProjectRootManager" project-jdk-type="JavaSDK"/></project>')
        (self.root / ".gitignore").write_text("/scripts/.venv/\n/scripts/.idea/\n")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        uv = self.bin / "uv"
        uv.write_text(f'''#!{sys.executable}
import os, pathlib, sys
if sys.argv[1] == "run":
    os.execv(sys.executable, [sys.executable, *sys.argv[2:]])
elif sys.argv[1] == "venv":
    root = pathlib.Path(sys.argv[-1])
    (root / "bin").mkdir(parents=True)
    (root / "bin/python").symlink_to(sys.executable)
    (root / "pyvenv.cfg").write_text("fixture")
else:
    sys.exit(17)
''')
        uv.chmod(0o755)
        self.environment = {
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "JETBRAINS_INSPECTION_TRUSTED_AUTO_OPEN_ROOTS": str(self.root),
        }
        self.args = Namespace(skip_preparation=False, force_preparation=False,
                              repository_preparation_timeout_ms=10000)

    def context(self, path="scripts"):
        preparation = jb_inspect.parse_structured_repository_preparation({"python": {
            "version": f"{sys.version_info.major}.{sys.version_info.minor}",
            "moduleName": "maintenance", "projectPath": path,
        }}, {})
        return {
            "worktree_root": str(self.root), "repo_path": str(self.root),
            "repository_preparation": preparation,
            "_repository_preparation_argv": preparation.pop("_argv"),
        }

    def prepare(self, context):
        with patch.dict(os.environ, self.environment):
            return jb_inspect.run_repository_preparation(self.args, context)

    def test_nested_generator_preserves_jvm_and_reuses_only_matching_model(self):
        before = self.jvm_model.read_bytes()
        context = self.context()
        prepared = self.prepare(context)
        self.assertEqual(prepared["execution_state"], jb_inspect.REPOSITORY_PREPARATION_SUCCEEDED)
        self.assertEqual(self.jvm_model.read_bytes(), before)
        self.assertTrue((self.project / ".venv/bin/python").is_file())
        module = self.project / ".idea/maintenance.iml"
        self.assertTrue(module.is_file())
        self.assertEqual(self.prepare(context)["execution_state"], jb_inspect.REPOSITORY_PREPARATION_REUSED)
        module.write_text("broken model")
        self.assertEqual(self.prepare(context)["execution_state"], jb_inspect.REPOSITORY_PREPARATION_SUCCEEDED)
        self.assertNotEqual(module.read_text(), "broken model")
        self.assertEqual(self.jvm_model.read_bytes(), before)

        nested = {"context": {"project_path": str(self.project)}, "repository_preparation": prepared}
        root = {"context": {"project_path": str(self.root)}, "repository_preparation": prepared}
        self.assertTrue(jb_inspect.prepared_python_sdk_discovery_pending(nested))
        self.assertFalse(jb_inspect.prepared_python_sdk_discovery_pending(root))

    def test_nested_script_changes_invalidate_command_fingerprint(self):
        script = self.project / "probe.py"
        script.write_text("# /// script\n# dependencies = []\n# ///\n")
        context = self.context()
        argv = context["_repository_preparation_argv"] + ["--pep723-script", "probe.py"]
        before = jb_inspect.repository_preparation_command_hash(argv, self.root)
        script.write_text('# /// script\n# dependencies = ["example"]\n# ///\n')
        self.assertNotEqual(before, jb_inspect.repository_preparation_command_hash(argv, self.root))

    def test_escaping_project_paths_refuse_before_execution(self):
        for value in ("../outside", "/outside", "scripts/../other", "scripts\\other", ""):
            with self.subTest(path=value), self.assertRaises(jb_inspect.InspectError):
                self.context(value)

    def test_symlink_escape_refuses_even_with_opt_out_or_reusable_receipt(self):
        context = self.context()
        self.prepare(context)
        moved = self.root / "previous"
        self.project.rename(moved)
        with tempfile.TemporaryDirectory() as outside:
            self.project.symlink_to(outside, target_is_directory=True)
            for skip in (False, True):
                self.args.skip_preparation = skip
                with self.subTest(skip=skip), self.assertRaises(jb_inspect.InspectError):
                    self.prepare(context)
            self.assertEqual(list(Path(outside).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
