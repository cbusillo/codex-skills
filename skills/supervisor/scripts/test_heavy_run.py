#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import io
import json
import multiprocessing
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import tarfile
import time
import unittest
from decimal import Decimal
from multiprocessing.process import BaseProcess
from pathlib import Path
from unittest.mock import patch

import heavy_run


class VolumeDocker(heavy_run.Docker):
    def __init__(self, root: Path) -> None:
        super().__init__(heavy_run.Slot(0, io.BytesIO(), "fixture"), io.StringIO(), "unix:///fixture")
        self.root = root
        self.volumes = {}
        self.creates = []

    def call(self, *args: str, timeout: int | None = 60) -> str:
        if args[0] == "info":
            return str(self.root)
        if args[0] == "create":
            self.creates.append(args)
            return args[args.index("--name") + 1]
        if args[:2] == ("volume", "ls"):
            return "\n".join(self.volumes)
        if args[:2] == ("volume", "inspect"):
            return json.dumps([self.volumes[args[-1]]])
        if args[:2] == ("volume", "create"):
            name = args[-1]
            labels, options = {}, {}
            for index, value in enumerate(args[:-1]):
                if value in {"--opt", "--label"}:
                    key, setting = args[index + 1].split("=", 1)
                    (labels if value == "--label" else options)[key] = setting
            backing = self.root / "volumes" / name / "_data"
            backing.mkdir(parents=True)
            self.volumes[name] = {"Name": name, "Driver": "local", "Options": options,
                                  "Labels": labels, "Mountpoint": str(backing)}
            return name
        if args[:2] == ("volume", "rm"):
            name = args[-1]
            if any(name + "/_data/" in volume["Options"].get("o", "") for volume in self.volumes.values()):
                raise RuntimeError("Writable backing still has an overlay consumer")
            shutil.rmtree(self.root / "volumes" / name)
            del self.volumes[name]
            return name
        raise AssertionError(args)


class OverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.lower = self.root / "lower"
        self.lower.mkdir(mode=0o755)
        (self.lower / "attachment").write_bytes(b"original attachment")
        (self.lower / "attachment").chmod(0o644)
        heavy_run.Docker.stage_overlay_input(self.lower)
        self.docker = VolumeDocker(self.root / "engine")
        self.copy = {"source": str(self.lower), "destination": "/workspace/filestore", "overlay": True}

    def test_shared_lower_gets_private_volumes_and_cleanup_preserves_original(self) -> None:
        before = heavy_run.overlay_lower_digest(self.lower)
        with patch.object(os, "uname", return_value=type("System", (), {"sysname": "Linux"})()):
            for name in ("first", "second"):
                copy = {**self.copy, "destination": f"/workspace/{name}"}
                self.docker.create({"name": name, "image": "fixture", "copies": [copy]}, "run", "1", "1024", self.root)
        self.assertEqual(len(self.docker.creates), 2)
        mounts = [args[args.index("--mount") + 1] for args in self.docker.creates]
        self.assertNotEqual(*mounts)
        backings = [Path(volume["Mountpoint"]) for volume in self.docker.volumes.values() if not volume["Options"]]
        self.assertEqual(len(backings), 2)
        (backings[0] / "upper/attachment").write_bytes(b"private change")
        self.assertFalse((backings[1] / "upper/attachment").exists())
        self.assertEqual(heavy_run.overlay_lower_digest(self.lower), before)
        self.docker.cleanup_overlay_volumes()
        self.assertFalse(self.docker.volumes)
        self.assertTrue(self.lower.is_dir())
        self.assertEqual((self.lower / "attachment").read_bytes(), b"original attachment")

    def test_containers_in_the_same_run_share_the_same_destination_upper(self) -> None:
        with patch.object(os, "uname", return_value=type("System", (), {"sysname": "Linux"})()):
            for name in ("web", "migration"):
                self.docker.create({"name": name, "image": "fixture", "copies": [self.copy]}, "run", "1", "1024", self.root)
        self.assertEqual(len(self.docker.volumes), 2)
        mounts = [args[args.index("--mount") + 1] for args in self.docker.creates]
        self.assertEqual(*mounts)

    def test_changed_lower_keeps_owned_volume_custody_for_recovery(self) -> None:
        with patch.object(os, "uname", return_value=type("System", (), {"sysname": "Linux"})()):
            self.docker.create_overlay(self.copy, "owned")
        (self.lower / "attachment").write_bytes(b"unexpected host change")
        with self.assertRaises(RuntimeError):
            self.docker.cleanup_overlay_volumes()
        self.assertEqual(len(self.docker.volumes), 2)

    def test_admission_reads_bytes_once_and_entry_reuses_the_full_identity(self) -> None:
        (self.lower / "empty").mkdir()
        (self.lower / "empty").chmod(0o777)
        expected = heavy_run.overlay_lower_digest(self.lower)
        with patch.object(Path, "open", autospec=True, side_effect=Path.open) as opened:
            hashes = self.docker.observe_overlay_lower(self.lower)
            with patch.object(os, "uname", return_value=type("System", (), {"sysname": "Linux"})()):
                self.docker.create_overlay(self.copy, "owned")
        self.assertEqual(hashes, {"attachment": heavy_run.hashlib.sha256(b"original attachment").hexdigest()})
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(self.docker.volumes["owned"]["Labels"][heavy_run.LOWER_DIGEST_LABEL], expected)
        self.docker.cleanup_overlay_volumes()
        self.assertFalse(self.docker.volumes)

    def test_mutation_after_admission_retains_custody_even_after_reconstruction(self) -> None:
        for mutation in ("bytes", "empty_directory", "mode", "inode"):
            with self.subTest(mutation=mutation):
                self.docker.observe_overlay_lower(self.lower)
                attachment = self.lower / "attachment"
                if mutation == "bytes":
                    attachment.write_bytes(b"changed before entry")
                elif mutation == "empty_directory":
                    (self.lower / "new-directory").mkdir()
                    (self.lower / "new-directory").chmod(0o777)
                elif mutation == "mode":
                    attachment.chmod(0o777)
                else:
                    replacement = self.root / "replacement"
                    replacement.write_bytes(attachment.read_bytes())
                    replacement.chmod(0o777)
                    replacement.replace(attachment)
                with patch.object(os, "uname", return_value=type("System", (), {"sysname": "Linux"})()):
                    self.docker.create_overlay(self.copy, mutation)
                recovered = VolumeDocker(self.docker.root)
                recovered.volumes = self.docker.volumes
                self.assertFalse(recovered.lower_digests)
                for docker in (self.docker, recovered):
                    with self.assertRaises(RuntimeError):
                        docker.cleanup_overlay_volumes()
                self.assertTrue(recovered.volumes)
                self.docker.volumes = {}

    def test_aliases_special_files_and_mount_option_injection_are_refused(self) -> None:
        alias = self.root / "alias"
        alias.symlink_to(self.lower, target_is_directory=True)
        injected = self.root / "lower,upperdir=other"
        injected.mkdir()
        for path in (alias, injected):
            with self.subTest(path=path), self.assertRaises(ValueError):
                heavy_run.overlay_lower_digest(path)
        (self.lower / "link").symlink_to(self.lower / "attachment")
        with self.assertRaises(ValueError):
            heavy_run.overlay_lower_digest(self.lower)
        bad = {**self.copy, "destination": "/workspace,src=other"}
        with self.assertRaises(ValueError):
            heavy_run.validate_manifest({"containers": [{"name": "job", "image": "fixture", "copies": [bad]}], "job": "job"})

    def test_overlay_refuses_non_linux_and_skips_the_container_copy(self) -> None:
        with patch.object(os, "uname", return_value=type("System", (), {"sysname": "Darwin"})()):
            with self.assertRaises(ValueError):
                self.docker.create_overlay(self.copy, "owned")
        with patch.object(subprocess, "Popen") as copy_process:
            self.docker.copy_input("fixture", self.copy)
        copy_process.assert_not_called()

    def test_data_filter_extraction_is_prepared_for_image_user_writes(self) -> None:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            archive.add(self.lower, arcname="filestore")
        extracted = self.root / "received"
        extracted.mkdir()
        stream.seek(0)
        with tarfile.open(fileobj=stream) as archive:
            archive.extractall(extracted, filter="data")
        lower = extracted / "filestore"
        with self.assertRaises(ValueError):
            heavy_run.overlay_lower_digest(lower)
        heavy_run.Docker.stage_overlay_input(lower)
        self.assertTrue(heavy_run.overlay_lower_digest(lower))
        self.assertEqual((lower / "attachment").read_bytes(), (self.lower / "attachment").read_bytes())


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
        self.binary = self.root / "bin"
        self.binary.mkdir()
        docker = self.binary / "docker"
        fixture = Path(__file__).with_name("fixtures") / "heavy-docker.py"
        docker.write_text(f"#!{sys.executable}\n" + fixture.read_text())
        docker.chmod(0o700)

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
                "PATH": f"{self.binary}{os.pathsep}{os.environ['PATH']}",
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
