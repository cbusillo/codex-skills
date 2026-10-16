#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline daily entry-point, delivery and interrupted-send recovery tests."""

import datetime as dt
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import services
import stranded_service as daily


class DailyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.root / "state"
        self.config = self.root / "daily.json"
        self.sender = self.root / "sender.py"
        self.sender.write_text(
            "import sys\nfrom pathlib import Path\n"
            "p = Path('received')\n"
            "with p.open('ab') as out: out.write(sys.stdin.buffer.read())\n"
            "print('https://discord.com/channels/1/2/3')\n"
            "raise SystemExit(int(Path('send-code').read_text()))\n"
        )
        # This fixture implements only uv's command boundary, without contacting
        # GitHub, Discord or the installed runtime.
        self.uv = self.root / "uv"
        self.uv.write_text(
            f"#!{sys.executable}\nimport sys, subprocess\nfrom pathlib import Path\n"
            "scripts = [i for i, a in enumerate(sys.argv) if a.endswith('.py')]\n"
            "i = scripts[0]\n"
            "if sys.argv[i].endswith('direction_audit.py'):\n"
            "    print(Path('payload').read_text(), end='')\n"
            "    raise SystemExit(int(Path('scan-code').read_text()))\n"
            "raise SystemExit(subprocess.run([sys.executable, *sys.argv[i:]]).returncode)\n"
        )
        self.uv.chmod(0o700)
        self.data = {"uv": str(self.uv), "catalog": str(self.root),
                     "state_dir": str(self.state), "scan_args": [],
                     "delivery_argv": [sys.executable, str(self.sender)], "hour": 7, "minute": 30}
        # Fixtures live under the temporary catalog working directory. Copy only
        # tracked runner sources so behavior never depends on installed state.
        runner_dir = self.root / "skills/supervisor/scripts"
        runner_dir.mkdir(parents=True)
        for module in (daily, services):
            source = Path(module.__file__)
            (runner_dir / source.name).write_bytes(source.read_bytes())
        self.payload = "Coverage incomplete: offline fixture\n"
        (self.root / "payload").write_text(self.payload)
        (self.root / "scan-code").write_text("0")
        (self.root / "send-code").write_text("0")
        self.save()

    def save(self):
        self.config.write_text(json.dumps(self.data))

    def entry(self):
        path = daily.render(self.config, self.root / "jobs")
        with path.open("rb") as stream:
            job = plistlib.load(stream)
        return subprocess.run(job["ProgramArguments"], cwd=job["WorkingDirectory"],
                              capture_output=True, text=True, timeout=10, check=False)

    def test_rendered_entry_delivers_incomplete_report_and_deduplicates(self):
        (self.root / "scan-code").write_text("3")
        result = self.entry()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "received").read_text(), self.payload)
        receipt = json.loads(next(self.state.glob("*.delivered.json")).read_text())
        self.assertEqual(receipt["scan_exit_code"], int((self.root / "scan-code").read_text()))
        self.assertIn("https://discord.com/", next(self.state.rglob("delivery.stdout")).read_text())
        self.assertEqual(self.entry().returncode, 0)
        self.assertEqual((self.root / "received").read_text(), self.payload)

    def test_delivery_failure_blocks_replay_until_reconciled(self):
        (self.root / "send-code").write_text("1")
        self.assertEqual(self.entry().returncode, 1)
        self.assertFalse(list(self.state.glob("*.delivered.json")))
        (self.root / "send-code").write_text("0")
        self.assertEqual(self.entry().returncode, 1)
        self.assertEqual((self.root / "received").read_text(), self.payload)
        cleared = subprocess.run([sys.executable, str(daily.__file__), "clear", "--config", str(self.config)],
                                 capture_output=True, timeout=5, check=False)
        self.assertEqual(cleared.returncode, 0)
        self.assertEqual(self.entry().returncode, 0)
        self.assertEqual((self.root / "received").read_text(), self.payload * 2)
        self.assertEqual(len(list(self.state.rglob("delivery.stdout"))), 2)
        self.assertTrue(all(path.read_text() for path in self.state.rglob("delivery.stdout")))

    def test_scan_error_is_retained_without_sending_and_next_run_can_recover(self):
        (self.root / "scan-code").write_text("2")
        self.assertEqual(self.entry().returncode, 1)
        self.assertFalse((self.root / "received").exists())
        self.assertEqual(next(self.state.rglob("report.md")).read_text(), self.payload)
        (self.root / "scan-code").write_text("0")
        self.assertEqual(self.entry().returncode, 0)

    def test_confirmed_send_reconciliation_resumes_schedule_without_replay(self):
        (self.root / "send-code").write_text("1")
        self.assertEqual(self.entry().returncode, 1)
        cleared = subprocess.run([sys.executable, str(daily.__file__), "clear", "--config", str(self.config),
                                  "--delivered-url", "https://discord.com/channels/1/2/3"],
                                 capture_output=True, timeout=5, check=False)
        self.assertEqual(cleared.returncode, 0)
        self.assertEqual(self.entry().returncode, 0)
        self.assertEqual((self.root / "received").read_text(), self.payload)

    def test_interrupted_delivery_reservation_and_lock_prevent_send(self):
        self.state.mkdir()
        marker = services.reconciliation_marker(self.state / "daily", "stranded")
        marker.touch()
        self.assertEqual(self.entry().returncode, 1)
        marker.unlink()
        with services.service_lock(self.state / "daily", "stranded"):
            self.assertEqual(self.entry().returncode, 1)
        self.assertFalse((self.root / "received").exists())

    def test_file_sender_receives_exact_retained_report_path(self):
        self.sender.write_text("import sys\nfrom pathlib import Path\nPath('received').write_bytes(Path(sys.argv[1]).read_bytes())\n")
        self.data["delivery_argv"].append("{report}")
        self.save()
        self.assertEqual(self.entry().returncode, 0)
        self.assertEqual((self.root / "received").read_text(), self.payload)

    def test_previous_day_receipt_does_not_suppress_today(self):
        self.state.mkdir()
        yesterday = dt.datetime.now().astimezone().date() - dt.timedelta(days=1)
        (self.state / f"{yesterday}.delivered.json").write_text("{}")
        self.assertEqual(self.entry().returncode, 0)
        self.assertEqual((self.root / "received").read_text(), self.payload)

    def test_invalid_config_refuses_before_rendering(self):
        for key, value in (("hour", 24), ("minute", True), ("delivery_argv", []),
                           ("uv", "relative"), ("scan_args", ["--stranded-format=json"])):
            with self.subTest(key=key):
                original = self.data[key]
                self.data[key] = value
                self.save()
                with self.assertRaises(ValueError):
                    daily.render(self.config, self.root / "jobs")
                self.data[key] = original

    def test_stopped_child_is_reaped(self):
        stopped = threading.Event()
        stopped.set()
        children = []
        original = subprocess.Popen

        def capture(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            return child

        with mock.patch.object(daily.subprocess, "Popen", side_effect=capture), self.assertRaises(TimeoutError):
            daily.command([sys.executable, "-c", "import time; time.sleep(60)"], self.root,
                          subprocess.DEVNULL, subprocess.DEVNULL, subprocess.DEVNULL, stopped, 5)
        self.assertIsNotNone(children[0].poll())
        with self.assertRaises(ProcessLookupError):
            os.killpg(children[0].pid, 0)


if __name__ == "__main__":
    unittest.main()
