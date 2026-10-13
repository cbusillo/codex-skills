#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Run copied helpers with local declared dependencies and inspect their cache footprint."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

RUNNER = Path(__file__).with_name("run-python-helper.sh")


class HelperCacheTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("uv"), "uv is required")
    def test_copied_helpers_import_declared_dependency_without_persistent_script_environments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            cache = root / "cache"
            wheel = root / "fixture_dependency-1.0-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("fixture_dependency.py", "value = 'dependency was imported'\n")
                archive.writestr("fixture_dependency-1.0.dist-info/METADATA",
                                 "Metadata-Version: 2.1\nName: fixture-dependency\nVersion: 1.0\n")
                archive.writestr("fixture_dependency-1.0.dist-info/WHEEL",
                                 "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
                archive.writestr("fixture_dependency-1.0.dist-info/RECORD", "")
            dependency = f"fixture-dependency @ {wheel.as_uri()}"
            source = ('# /// script\n# requires-python = ">=3.12"\n'
                      f'# dependencies = [{json.dumps(dependency)}]\n# ///\n'
                      'import fixture_dependency,json,sys\n'
                      'from pathlib import Path\n'
                      'print(json.dumps([fixture_dependency.value, __file__,sys.argv[1:],str(Path.cwd())]))\n')
            env = {key: value for key, value in os.environ.items() if not key.startswith("UV_") and key != "VIRTUAL_ENV"}
            env.update(UV_CACHE_DIR=str(cache), UV_OFFLINE="1", UV_NO_CONFIG="1", UV_PYTHON_DOWNLOADS="never")
            first_entries = None
            for name in ("first-worktree", "second-worktree"):
                worktree = root / name
                worktree.mkdir()
                script = worktree / "helper.py"
                script.write_text(source)
                result = subprocess.run([str(RUNNER), str(script), "argument with spaces"],
                                        cwd=worktree, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data, ["dependency was imported", str(script), ["argument with spaces"], str(worktree)])
                bucket = cache / "environments-v2"
                entries = {x.name for x in bucket.iterdir()} if bucket.exists() else set()
                if first_entries is None:
                    first_entries = entries
                else:
                    self.assertEqual(entries, first_entries)

    def test_helper_failure_is_returned_to_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "failure.py"
            script.write_text('# /// script\n# dependencies = []\n# ///\nraise SystemExit(23)\n')
            result = subprocess.run([str(RUNNER), str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 23, result.stderr)


if __name__ == "__main__":
    unittest.main()
