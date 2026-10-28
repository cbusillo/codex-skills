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
from decimal import Decimal
from multiprocessing.process import BaseProcess
from pathlib import Path
from unittest.mock import patch

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
    def test_configured_executor_replaces_the_process_with_absolute_input_paths(self) -> None:
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"containers": [{"name": "job", "image": "fixture"}], "job": "job"}))
        config = self.root / heavy_run.CONFIG_PATH
        config.parent.mkdir(parents=True)
        arguments = self.root / "adapter-arguments.json"
        adapter = (
            f"import json,sys; from pathlib import Path; Path({str(arguments)!r}).write_text(json.dumps(sys.argv[1:])); sys.exit(23)"
        )
        executor = [sys.executable, "-c", adapter, "--fixed-option"]
        config.write_text(json.dumps({"executor": executor}))
        evidence = self.root / "evidence"
        entry = (
            f"import sys; from pathlib import Path; sys.path.insert(0, {str(Path(__file__).parent)!r}); "
            f"import heavy_run; raise SystemExit(heavy_run.main(home=Path({str(self.root)!r})))"
        )
        result = subprocess.run(
            [sys.executable, "-c", entry, "--manifest", str(manifest), "--evidence", str(evidence)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(
            json.loads(arguments.read_text()),
            ["--fixed-option", "--manifest", str(manifest.resolve()), "--evidence", str(evidence.resolve())],
        )
        self.assertFalse((self.root / heavy_run.STATE_PATH).exists())
        self.assertFalse(evidence.exists())

    def test_invalid_executor_settings_cannot_fall_back_to_local_execution(self) -> None:
        config = self.root / heavy_run.CONFIG_PATH
        config.parent.mkdir(parents=True)
        for executor in ([], "adapter", [""], ["adapter", 1], ["adapter", "bad\x00argument"]):
            with self.subTest(executor=executor):
                config.write_text(json.dumps({"executor": executor}))
                with self.assertRaises(ValueError):
                    heavy_run.Settings.load(self.root)

    def test_preparation_and_evidence_delivery_hold_the_execution_slot(self) -> None:
        events = []
        evidence = self.root / "evidence"
        manifest = {"containers": [{"name": "job", "image": "fixture"}], "job": "job"}

        def require_slot_held() -> None:
            with heavy_run.locked_file(self.root / heavy_run.STATE_PATH / "slot-0.lock") as held_slot:
                self.assertFalse(heavy_run.try_lock(held_slot))

        def prepare(_docker: heavy_run.Docker) -> dict:
            require_slot_held()
            events.append("prepared")
            return manifest

        def complete(receipt: dict) -> None:
            require_slot_held()
            self.assertEqual(json.loads((evidence / "result.json").read_text()), receipt)
            events.append("delivered")

        with patch.object(heavy_run, "local_endpoint", return_value="unix:///fixture.sock"), patch.object(heavy_run, "recover"):
            with patch.object(heavy_run.Docker, "run", return_value=(0, {})), patch.object(heavy_run.Docker, "cleanup") as cleanup:
                self.assertEqual(heavy_run.run({}, evidence, heavy_run.Settings(), self.root, prepare, complete), 0)
        cleanup.assert_called_once()
        self.assertEqual(events, ["prepared", "delivered"])
        with heavy_run.locked_file(self.root / heavy_run.STATE_PATH / "slot-0.lock") as handle:
            self.assertTrue(heavy_run.try_lock(handle))

    def test_canceled_preparation_cleans_and_delivers_receipt_without_starting_job(self) -> None:
        evidence = self.root / "evidence"
        delivered = []

        def prepare(_docker: heavy_run.Docker) -> dict:
            raise heavy_run.Canceled(signal.SIGTERM)

        with patch.object(heavy_run, "local_endpoint", return_value="unix:///fixture.sock"), patch.object(heavy_run, "recover"):
            with patch.object(heavy_run.Docker, "run") as execute, patch.object(heavy_run.Docker, "cleanup") as cleanup:
                result = heavy_run.run({}, evidence, heavy_run.Settings(), self.root, prepare, delivered.append)
        self.assertEqual(result, 128 + signal.SIGTERM)
        execute.assert_not_called()
        cleanup.assert_called_once()
        self.assertTrue(delivered[0]["cleanup_complete"])
        self.assertFalse(list((self.root / heavy_run.STATE_PATH).glob("custody-*")))

    def test_delivery_failure_preserves_the_completed_job_receipt(self) -> None:
        evidence = self.root / "evidence"
        manifest = {"containers": [{"name": "job", "image": "fixture"}], "job": "job"}

        def complete(_receipt: dict) -> None:
            raise BrokenPipeError("fixture disconnected")

        with patch.object(heavy_run, "local_endpoint", return_value="unix:///fixture.sock"), patch.object(heavy_run, "recover"):
            with patch.object(heavy_run.Docker, "run", return_value=(0, {})), patch.object(heavy_run.Docker, "cleanup"):
                result = heavy_run.run(manifest, evidence, heavy_run.Settings(), self.root, complete=complete)
        self.assertNotEqual(result, 0)
        receipt = json.loads((evidence / "result.json").read_text())
        self.assertEqual(receipt["exit_code"], 0)
        self.assertTrue(receipt["cleanup_complete"])
        self.assertTrue((evidence / "delivery-error.json").exists())

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

    def launch(
        self,
        name: str,
        *,
        fail: bool = False,
        hold_create: bool = False,
        services: int = 0,
        endpoint: str = "unix:///fixture/docker.sock",
        job_exit: int = 0,
        fail_output: bool = False,
        home_name: str = "home",
    ) -> subprocess.Popen[str]:
        home = self.root / home_name
        config = home / heavy_run.CONFIG_PATH
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"cpus": 2, "memory": 1024 * 1024 * 1024, "slots": 1}))
        binary = self.root / "bin"
        binary.mkdir(exist_ok=True)
        docker = binary / "docker"
        fixture = Path(__file__).with_name("fixtures") / "heavy-docker.py"
        docker.write_text(f"#!{sys.executable}\n" + fixture.read_text())
        docker.chmod(0o700)
        source = self.root / "source"
        source.mkdir(mode=0o700, exist_ok=True)
        (source / "private.txt").write_text("private-test-output")
        (source / "private.txt").chmod(0o600)
        (source / "run.sh").write_text("exit 0\n")
        (source / "run.sh").chmod(0o700)
        manifest = self.root / f"{name}.json"
        containers = [
            {
                "name": "job",
                "image": "fixture-image",
                "command": [name],
                "copies": [{"source": str(source), "destination": "/workspace"}],
                "environment": {"FIXTURE_SECRET": "private-env-value"},
            }
        ]
        for index in range(services):
            containers.insert(0, {"name": f"service-{index}", "image": "fixture-image", "command": [f"service-{index}"]})
        manifest.write_text(
            json.dumps(
                {
                    "containers": containers,
                    "job": "job",
                    "outputs": [{"container": "job", "source": "/result", "name": "result"}],
                }
            )
        )
        entry = (
            f"import sys; from pathlib import Path; sys.path.insert(0, {str(Path(__file__).parent)!r}); "
            f"import heavy_run; raise SystemExit(heavy_run.main(home=Path({str(home)!r})))"
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                entry,
                "--manifest",
                str(manifest),
                "--evidence",
                str(self.root / name),
            ],
            env={
                **os.environ,
                "PATH": f"{binary}{os.pathsep}{os.environ['PATH']}",
                "HEAVY_FIXTURE": str(self.root / "docker"),
                "HEAVY_FAIL_CLEANUP": str(int(fail)),
                "HEAVY_HOLD_CREATE": str(int(hold_create)),
                "HEAVY_ENDPOINT": endpoint,
                "HEAVY_EXIT_CODE": str(job_exit),
                "HEAVY_FAIL_OUTPUT": str(int(fail_output)),
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
        process = self.launch("normal", services=2)
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
        allocated = sum(Decimal(call[call.index("--cpus") + 1]) for call in creates)
        self.assertLessEqual(allocated, Decimal(str(settings.cpus)))
        self.assertLess(Decimal(str(settings.cpus)) - allocated, Decimal(len(creates)) / 10**9)
        self.assertLessEqual(sum(int(call[call.index("--memory") + 1]) for call in creates), settings.memory)
        create = creates[-1]
        self.assertNotIn("--mount", create)
        self.assertNotIn("--volume", create)
        self.assertNotIn("--workdir", create)
        self.assertNotIn("private-env-value", json.dumps(calls))
        self.assertFalse((self.root / "normal/job.env").exists())
        self.assertEqual(int((self.root / "docker/env-mode-normal").read_text()), 0o600)
        copied = [entry for path in (self.root / "docker").glob("copy-*") for entry in json.loads(path.read_text())]
        private = next(entry for entry in copied if entry["name"].endswith("private.txt"))
        script = next(entry for entry in copied if entry["name"].endswith("run.sh"))
        manifest = json.loads((self.root / "normal.json").read_text())
        destination = manifest["containers"][-1]["copies"][0]["destination"].lstrip("/")
        directory = next(entry for entry in copied if entry["name"] == destination)
        self.assertEqual(directory["mode"] & 0o007, 0o007)
        self.assertTrue(all(entry["name"] == destination or entry["name"].startswith(f"{destination}/") for entry in copied))
        self.assertEqual(private["mode"] & 0o006, 0o006)
        self.assertEqual(script["mode"] & 0o001, 0o001)
        self.assertEqual((self.root / "source/private.txt").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "source/run.sh").stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / "normal/run.log").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "normal").stat().st_mode & 0o777, 0o700)
        self.assertFalse(list((self.root / "docker").glob("container-*")))

    def test_crash_reconciles_running_container_before_next_start(self) -> None:
        crashed = self.launch("crashed")
        wait_for(self.root / "docker/started-crashed")
        crashed.kill()
        crashed.communicate(timeout=10)
        successor = self.launch("successor", endpoint="unix:///other-fixture.sock")
        wait_for(self.root / "docker/started-successor")
        self.assertFalse((self.root / "docker/running-crashed").exists())
        (self.root / "docker/release-successor").touch()
        successor.communicate(timeout=15)
        self.assertEqual(successor.returncode, 0)
        self.assertFalse(list((self.root / "docker").glob("container-*")))
        self.assertIn("private-test-output:crashed", (self.root / "crashed/recovered.log").read_text())
        self.assertNotIn("private-test-output:crashed", (self.root / "successor/run.log").read_text())

    def test_inflight_docker_child_holds_lock_after_parent_crash(self) -> None:
        crashed = self.launch("creating", hold_create=True)
        wait_for(self.root / "docker/creating-creating")
        crashed.kill()
        crashed.communicate(timeout=10)
        try:
            with heavy_run.locked_file(self.root / "home" / heavy_run.STATE_PATH / "slot-0.lock") as handle:
                self.assertFalse(heavy_run.try_lock(handle))
            successor = self.launch("successor")
            wait_for(self.root / "successor/run.log")
        finally:
            (self.root / "docker/finish-creating").touch()
        wait_for(self.root / "docker/started-successor")
        (self.root / "docker/release-successor").touch()
        successor.communicate(timeout=15)
        self.assertEqual(successor.returncode, 0)
        self.assertFalse(list((self.root / "docker").glob("container-*")))
        self.assertFalse((self.root / "creating/job.env").exists())

    def test_distinct_queues_do_not_remove_each_others_work(self) -> None:
        first = self.launch("first")
        wait_for(self.root / "docker/started-first")
        second = self.launch("second", home_name="second-home")
        wait_for(self.root / "docker/started-second")
        (self.root / "docker/release-second").touch()
        second.communicate(timeout=15)
        self.assertEqual(second.returncode, 0)
        self.assertTrue((self.root / "docker/running-first").exists())
        (self.root / "docker/release-first").touch()
        first.communicate(timeout=15)
        self.assertEqual(first.returncode, 0)
        self.assertFalse(list((self.root / "docker").glob("container-*")))

    def test_cancel_cleans_up_and_failed_cleanup_prevents_successor(self) -> None:
        canceled = self.launch("canceled")
        wait_for(self.root / "docker/started-canceled")
        canceled.send_signal(signal.SIGHUP)
        canceled.communicate(timeout=15)
        self.assertEqual(canceled.returncode, 128 + signal.SIGHUP)
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
        custody = self.root / "home" / heavy_run.STATE_PATH / "custody-0"
        self.assertEqual(heavy_run.Custody.load(custody).evidence, (self.root / "failed").resolve())

    def test_queued_cancel_and_startup_failure_leave_receipts_without_custody(self) -> None:
        holder = self.launch("holder")
        wait_for(self.root / "docker/started-holder")
        waiter = self.launch("waiter")
        wait_for(self.root / "waiter/run.log")
        waiter.send_signal(signal.SIGTERM)
        waiter.communicate(timeout=10)
        self.assertEqual(waiter.returncode, 128 + signal.SIGTERM)
        self.assertTrue(json.loads((self.root / "waiter/result.json").read_text())["cleanup_complete"])
        (self.root / "docker/release-holder").touch()
        holder.communicate(timeout=15)
        remote = self.launch("remote", endpoint="tcp://fixture.invalid:1234")
        stdout, stderr = remote.communicate(timeout=10)
        self.assertNotEqual(remote.returncode, 0)
        self.assertEqual(stderr, "")
        self.assertFalse(json.loads(stdout)["ok"])
        self.assertTrue(json.loads((self.root / "remote/result.json").read_text())["cleanup_complete"])
        self.assertFalse(list((self.root / "home" / heavy_run.STATE_PATH).glob("custody-*")))

    def test_nonzero_job_exit_survives_missing_output_and_cleanup(self) -> None:
        expected = 23
        process = self.launch("failed-job", job_exit=expected, fail_output=True)
        wait_for(self.root / "docker/started-failed-job")
        (self.root / "docker/release-failed-job").touch()
        process.communicate(timeout=15)
        self.assertEqual(process.returncode, expected)
        result = json.loads((self.root / "failed-job/result.json").read_text())
        self.assertEqual(result["exit_code"], expected)
        self.assertIn("error", result)
        self.assertTrue(result["cleanup_complete"])

    def test_malformed_input_and_too_small_budgets_refuse_before_allocation(self) -> None:
        container = {"name": "job", "image": "fixture-image"}
        invalid = [None, [], {"containers": [None]}, {"containers": [{**container, "environment": []}], "job": "job"}]
        for source, destination in (([], "/workspace"), (str(self.root), [])):
            invalid.append({"containers": [{**container, "copies": [{"source": source, "destination": destination}]}], "job": "job"})
        for manifest in invalid:
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                heavy_run.validate_manifest(manifest)
        duplicate = {"container": "job", "name": "same", "source": "/result"}
        with self.assertRaises(ValueError):
            heavy_run.validate_manifest({"containers": [container], "job": "job", "outputs": [duplicate, duplicate]})
        for settings in (heavy_run.Settings(memory=heavy_run.MIN_MEMORY), heavy_run.Settings(cpus=heavy_run.MIN_NANO_CPUS / 10**9)):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                settings.limits(2)


if __name__ == "__main__":
    unittest.main()
