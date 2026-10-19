#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Prove native cache routing without importing caller project settings."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which("uv"), "uv required")
class CacheRoutingTests(unittest.TestCase):
    def test_user_cache_and_explicit_override_preserve_cwd_and_args(self):
        launcher = Path(__file__).with_name("github-python")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = root / "config/uv"
            config.mkdir(parents=True)
            configured = root / "configured"
            (config / "uv.toml").write_text(f'cache-dir = "{configured}"\n')
            # A caller project config would fail parsing if discovery leaked in.
            caller = root / "caller"
            caller.mkdir()
            (caller / "uv.toml").write_text("not valid TOML\n")
            script = root / "probe.py"
            script.write_text('import json,os,sys; print(json.dumps([os.environ.get("UV_CACHE_DIR"),os.getcwd(),sys.argv[1:]]))\n')
            env = {**os.environ, "XDG_CONFIG_HOME": str(config.parent), "UV_PYTHON_DOWNLOADS": "never"}
            env.pop("UV_CACHE_DIR", None)
            env.pop("HOME", None)
            for override in (None, root / "override"):
                env["UV_NO_CACHE"] = "1" if override is not None else "0"
                if override is not None:
                    env["UV_CACHE_DIR"] = str(override)
                process = subprocess.run([str(launcher), str(script), "space value"], cwd=caller,
                                         env=env, text=True, capture_output=True, check=True)
                payload = json.loads(process.stdout)
                self.assertEqual(payload[1:], [str(caller), ["space value"]])
                expected = override or configured
                self.assertEqual(payload[0], str(expected))
                if override is None:
                    self.assertTrue(expected.is_dir())
                self.assertFalse((caller / ".venv").exists())


if __name__ == "__main__":
    unittest.main()
