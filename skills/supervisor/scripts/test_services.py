#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise generated service entry points with bounded offline child fixtures."""

import json
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import services


class ServicesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "config.json"
        self.fixture = self.root / "child.py"
        self.data = {
            "uv": shutil.which("uv"),
            "services": {name: {"argv": [sys.executable, str(self.fixture)],
                                "cwd": str(self.root), "restart_seconds": 0.01}
                         for name in services.SERVICES},
        }

    def save(self):
        self.config.write_text(json.dumps(self.data))

    def test_rendered_job_restarts_clean_deadlines_and_stops_on_failure(self):
        self.fixture.write_text(
            "from pathlib import Path\n"
            "p = Path('passes.txt')\n"
            "previous = p.read_text() if p.exists() else ''\n"
            "p.write_text(previous + 'pass\\n')\n"
            "raise SystemExit(4 if previous.count('pass') == 2 else 0)\n"
        )
        self.save()
        catalog = Path(__file__).resolve().parents[3]
        output = services.render(self.config, catalog, self.root / "jobs")
        jobs = list(output.glob("*.plist"))
        self.assertEqual(len(jobs), len(self.data["services"]))
        with (output / f"{services.LABEL_PREFIX}.enqueue.plist").open("rb") as stream:
            job = plistlib.load(stream)
        result = subprocess.run(job["ProgramArguments"], cwd=job["WorkingDirectory"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual((self.root / "passes.txt").read_text().splitlines(), ["pass"] * 3)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([row["exit_code"] for row in events if row["event"] == "completed"],
                         [0, 0, 4])
        self.assertEqual(events[-1]["event"], "reconciliation_required")

    def test_sigterm_stops_active_child_and_leaves_no_restart(self):
        self.fixture.write_text(
            "from pathlib import Path\nimport signal\nimport time\n"
            "def stop(*_):\n    Path('terminated').touch()\n    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\nPath('started').touch()\n"
            "while True: time.sleep(0.1)\n"
        )
        self.save()
        runner = subprocess.Popen(
            [sys.executable, str(Path(services.__file__)), "run", "--config",
             str(self.config), "--service", "foreign"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(lambda: runner.kill() if runner.poll() is None else None)
        until = time.monotonic() + 5
        while not (self.root / "started").exists() and time.monotonic() < until:
            time.sleep(0.01)
        self.assertTrue((self.root / "started").exists())
        runner.send_signal(signal.SIGTERM)
        stdout, stderr = runner.communicate(timeout=5)
        self.assertEqual(runner.returncode, 0, stderr)
        self.assertTrue((self.root / "terminated").exists())
        self.assertEqual(sum(json.loads(line)["event"] == "started"
                             for line in stdout.splitlines()), 1)

    def test_duplicate_service_refuses_before_launch(self):
        self.save()
        with services.service_lock(self.config, "drive"):
            with self.assertRaises(ValueError):
                with services.service_lock(self.config, "drive"):
                    self.fail("duplicate service acquired the lock")

    def test_invalid_config_cannot_render_or_run(self):
        for change in (
            lambda: self.data["services"]["enqueue"].update(argv="a shell command"),
            lambda: self.data["services"]["drive"].update(restart_seconds=0),
            lambda: self.data["services"]["foreign"].update(cwd="relative"),
        ):
            original = json.loads(json.dumps(self.data))
            change()
            self.save()
            with self.assertRaises(ValueError):
                services.load_config(self.config)
            self.data = original


if __name__ == "__main__":
    unittest.main()
