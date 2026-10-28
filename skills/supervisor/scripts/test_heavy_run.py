#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import json
import multiprocessing
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from multiprocessing.process import BaseProcess
from pathlib import Path

import heavy_run


def wait_for(path: Path) -> None:
    deadline = time.monotonic() + 15
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out waiting for {path}")
        time.sleep(0.02)


def take_slot(root: Path, name: str, slots: int) -> None:
    with heavy_run.RunQueue(root, slots) as queue:
        (root / f"{name}.queued").touch()
        with queue.acquire() as slot:
            (root / f"{name}.admitted").write_text(str(slot.index))
            wait_for(root / f"{name}.release")


class HeavyRunTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.context = multiprocessing.get_context("spawn")

    def worker(self, name: str, slots: int = 1) -> BaseProcess:
        process = self.context.Process(target=take_slot, args=(self.root, name, slots))
        process.start()
        self.addCleanup(self.stop, process)
        wait_for(self.root / f"{name}.queued")
        return process

    @staticmethod
    def stop(process: BaseProcess) -> None:
        if process.is_alive():
            process.kill()
        process.join(5)

    def test_fifo_exclusion_and_killed_waiter(self) -> None:
        first = self.worker("first")
        wait_for(self.root / "first.admitted")
        canceled = self.worker("canceled")
        second = self.worker("second")
        third = self.worker("third")
        self.assertFalse((self.root / "second.admitted").exists())
        canceled.kill()
        canceled.join(5)
        (self.root / "first.release").touch()
        first.join(5)
        wait_for(self.root / "second.admitted")
        self.assertFalse((self.root / "third.admitted").exists())
        (self.root / "second.release").touch()
        second.join(5)
        wait_for(self.root / "third.admitted")
        (self.root / "third.release").touch()
        third.join(5)
        self.assertEqual([first.exitcode, second.exitcode, third.exitcode], [0, 0, 0])

    def test_kernel_releases_crashed_holder_and_configured_slots(self) -> None:
        first = self.worker("first", slots=2)
        second = self.worker("second", slots=2)
        wait_for(self.root / "first.admitted")
        wait_for(self.root / "second.admitted")
        self.assertNotEqual((self.root / "first.admitted").read_text(), (self.root / "second.admitted").read_text())
        third = self.worker("third", slots=2)
        self.assertFalse((self.root / "third.admitted").exists())
        first.kill()
        first.join(5)
        wait_for(self.root / "third.admitted")
        (self.root / "second.release").touch()
        (self.root / "third.release").touch()
        second.join(5)
        third.join(5)
        self.assertEqual([second.exitcode, third.exitcode], [0, 0])

    def test_resize_refuses_live_or_unreconciled_work(self) -> None:
        worker = self.worker("first")
        wait_for(self.root / "first.admitted")
        with self.assertRaises(ValueError), heavy_run.RunQueue(self.root, 2):
            self.fail("Must not resize a busy queue")
        worker.kill()
        worker.join(5)
        (self.root / "custody-0").touch()
        with self.assertRaises(ValueError), heavy_run.RunQueue(self.root, 2):
            self.fail("Must not abandon orphaned Docker work during resize")
        (self.root / "custody-0").unlink()
        with heavy_run.RunQueue(self.root, 2) as queue, queue.acquire() as slot:
            self.assertIn(slot.index, range(queue.slots))

    def launch(self, name: str, *, fail: bool = False, hold_create: bool = False, service: bool = False) -> subprocess.Popen[str]:
        home = self.root / "home"
        config = home / ".config/codex-skills/heavy-run.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"cpus": 2, "memory": 1024 * 1024 * 1024, "slots": 1}))
        binary = self.root / "bin"
        binary.mkdir(exist_ok=True)
        docker = binary / "docker"
        fixture = Path(__file__).with_name("fixtures") / "heavy-docker.py"
        docker.write_text(f"#!{sys.executable}\n" + fixture.read_text())
        docker.chmod(0o700)
        source = self.root / "source"
        source.mkdir(exist_ok=True)
        (source / "private.txt").write_text("private-test-output")
        manifest = self.root / f"{name}.json"
        containers = [
            {
                "name": "job",
                "image": "fixture-image",
                "command": [name],
                "copies": [{"source": str(source), "destination": "/workspace"}],
            }
        ]
        if service:
            containers.insert(0, {"name": "db", "image": "fixture-image", "command": ["service"]})
        manifest.write_text(
            json.dumps(
                {
                    "containers": containers,
                    "job": "job",
                    "outputs": [{"container": "job", "source": "/result", "name": "result"}],
                }
            )
        )
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("heavy_run.py")),
                "--manifest",
                str(manifest),
                "--evidence",
                str(self.root / name),
            ],
            env={
                **os.environ,
                "HOME": str(home),
                "PATH": f"{binary}{os.pathsep}{os.environ['PATH']}",
                "HEAVY_FIXTURE": str(self.root / "docker"),
                "HEAVY_FAIL_CLEANUP": str(int(fail)),
                "HEAVY_HOLD_CREATE": str(int(hold_create)),
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.reap, process)
        return process

    @staticmethod
    def reap(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)

    def test_container_caps_copy_private_output_and_cleanup(self) -> None:
        process = self.launch("normal", service=True)
        wait_for(self.root / "docker/started-normal")
        (self.root / "docker/release-normal").touch()
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertNotIn("private-test-output", stdout + stderr)
        receipt = json.loads((self.root / "normal/result.json").read_text())
        self.assertTrue(receipt["cleanup_complete"])
        self.assertEqual(receipt["exit_code"], 0)
        calls = [json.loads(line) for line in (self.root / "docker/calls").read_text().splitlines()]
        creates = [call for call in calls if call[0] == "create"]
        settings = heavy_run.Settings.load(self.root / "home")
        self.assertEqual(sum(float(call[call.index("--cpus") + 1]) for call in creates), settings.cpus)
        self.assertEqual(sum(int(call[call.index("--memory") + 1]) for call in creates), settings.memory)
        create = creates[-1]
        self.assertNotIn("--mount", create)
        self.assertNotIn("--volume", create)
        self.assertTrue(any(call[0] == "cp" for call in calls))
        self.assertEqual((self.root / "normal/run.log").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "normal").stat().st_mode & 0o777, 0o700)
        self.assertFalse(list((self.root / "docker").glob("container-*")))

    def test_crash_reconciles_running_container_before_next_start(self) -> None:
        crashed = self.launch("crashed")
        wait_for(self.root / "docker/started-crashed")
        crashed.kill()
        crashed.communicate(timeout=10)
        successor = self.launch("successor")
        wait_for(self.root / "docker/started-successor")
        self.assertFalse((self.root / "docker/running-crashed").exists())
        (self.root / "docker/release-successor").touch()
        successor.communicate(timeout=15)
        self.assertEqual(successor.returncode, 0)
        self.assertFalse(list((self.root / "docker").glob("container-*")))

    def test_inflight_docker_child_holds_lock_after_parent_crash(self) -> None:
        crashed = self.launch("creating", hold_create=True)
        wait_for(self.root / "docker/creating-creating")
        crashed.kill()
        crashed.communicate(timeout=10)
        successor = self.launch("successor")
        wait_for(self.root / "successor/run.log")
        self.assertFalse((self.root / "docker/started-successor").exists())
        (self.root / "docker/finish-creating").touch()
        wait_for(self.root / "docker/started-successor")
        (self.root / "docker/release-successor").touch()
        successor.communicate(timeout=15)
        self.assertEqual(successor.returncode, 0)
        self.assertFalse(list((self.root / "docker").glob("container-*")))

    def test_cancel_cleans_up_and_failed_cleanup_prevents_successor(self) -> None:
        canceled = self.launch("canceled")
        wait_for(self.root / "docker/started-canceled")
        canceled.send_signal(signal.SIGTERM)
        canceled.communicate(timeout=15)
        self.assertEqual(canceled.returncode, 128 + signal.SIGTERM)
        self.assertFalse(list((self.root / "docker").glob("container-*")))
        failed = self.launch("failed", fail=True)
        wait_for(self.root / "docker/started-failed")
        (self.root / "docker/release-failed").touch()
        failed.communicate(timeout=15)
        self.assertNotEqual(failed.returncode, 0)
        successor = self.launch("blocked", fail=True)
        successor.communicate(timeout=15)
        self.assertNotEqual(successor.returncode, 0)
        self.assertFalse((self.root / "docker/started-blocked").exists())


if __name__ == "__main__":
    unittest.main()
