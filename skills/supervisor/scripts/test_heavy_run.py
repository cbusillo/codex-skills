#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import errno
import hashlib
import io
import json
import multiprocessing
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from decimal import Decimal
from functools import partial
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any, BinaryIO
from unittest.mock import patch

import heavy_run


class ResultCollectionTests(unittest.TestCase):
    def test_missing_linked_and_unreadable_references_transfer_guest_bytes(self) -> None:
        original_digest = hashlib.file_digest
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            guest = root / "guest"
            guest.mkdir()
            (guest / "file").write_bytes(b"result")
            inventory = heavy_run.result_inventory(guest)
            for case in ("missing", "linked", "unreadable", "loop"):
                reference = root / f"reference-{case}"
                if case == "loop":
                    reference.symlink_to(reference)
                elif case != "missing":
                    reference.mkdir()
                    if case == "linked":
                        (reference / "file").symlink_to(guest / "file")
                    else:
                        (reference / "file").write_bytes(b"result")

                def digest(
                    stream: BinaryIO, algorithm: str, *, blocked: Path | None = reference / "file" if case == "unreadable" else None
                ) -> Any:
                    if blocked is not None and stream.name == str(blocked):
                        raise PermissionError("fixture reference read failure")
                    return original_digest(stream, algorithm)

                destination = root / case
                stream = io.BytesIO(json.dumps(inventory).encode() + b"\nresult")
                with patch.object(hashlib, "file_digest", side_effect=digest):
                    report = heavy_run.receive_result(stream, io.BytesIO(), destination, reference)
                self.assertEqual((destination / "file").read_bytes(), b"result")
                self.assertEqual(report["cloned_files"], 0)
                self.assertEqual(report["transferred_bytes"], len(b"result"))

    def test_collection_clones_unchanged_files_and_transfers_only_changes_with_full_verification(self) -> None:
        with patch.object(heavy_run, "clone_input", side_effect=shutil.copyfile):
            self.check_delta_and_full_collection()

    @unittest.skipUnless(sys.platform == "darwin", "Native clone fixture requires macOS")
    def test_native_collection_clones_are_independent_and_match_full_transfer(self) -> None:
        self.check_delta_and_full_collection()

    def check_delta_and_full_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            reference = root / "reference"
            reference.mkdir()
            (reference / "same").write_bytes(b"unchanged" * 4096)
            (reference / "changed").write_bytes(b"old bytes")
            (reference / "removed").write_bytes(b"removed")
            guest = root / "guest"
            shutil.copytree(reference, guest)
            (guest / "changed").write_bytes(b"new bytes")
            (guest / "added").write_bytes(b"added")
            (guest / "removed").unlink()
            (guest / "empty").mkdir()
            guest.chmod(0o1777)
            (guest / "same").chmod(0o444)
            expected = heavy_run.result_inventory(guest)
            for local_reference in (reference, None):
                with self.subTest(reference=local_reference):
                    destination = root / ("delta" if local_reference else "full")
                    sender, receiver = socket.socketpair()
                    with (
                        sender,
                        receiver,
                        sender.makefile("rb") as remote_requests,
                        sender.makefile("wb") as remote_stream,
                        receiver.makefile("rb") as local_stream,
                        receiver.makefile("wb") as local_requests,
                    ):
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            sent = pool.submit(heavy_run.send_result, guest, remote_stream, remote_requests)
                            result = heavy_run.receive_result(local_stream, local_requests, destination, local_reference)
                            sent.result(timeout=5)
                    self.assertEqual(
                        {
                            name: {key: value for key, value in entry.items() if key != "mode"}
                            for name, entry in heavy_run.result_inventory(destination).items()
                        },
                        {name: {key: value for key, value in entry.items() if key != "mode"} for name, entry in expected.items()},
                    )
                    expected_bytes = sum((guest / name).stat().st_size for name in ("changed", "added"))
                    if local_reference is None:
                        expected_bytes += (guest / "same").stat().st_size
                    self.assertEqual(result["transferred_bytes"], expected_bytes)
                    self.assertEqual(result["cloned_files"], int(local_reference is not None))
            self.assertEqual(heavy_run.result_inventory(root / "delta"), heavy_run.result_inventory(root / "full"))
            (root / "delta/same").chmod(0o600)
            (root / "delta/same").write_bytes(b"independent")
            self.assertEqual((reference / "same").read_bytes(), (guest / "same").read_bytes())
            self.assertEqual((reference / "changed").read_bytes(), b"old bytes")
            self.assertTrue((reference / "removed").exists())

    def test_corruption_and_truncation_preserve_reference_and_partial_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            reference = root / "reference"
            reference.mkdir()
            (reference / "file").write_bytes(b"original")
            guest = root / "guest"
            guest.mkdir()
            (guest / "file").write_bytes(b"expected")
            header = json.dumps(heavy_run.result_inventory(guest)).encode() + b"\n"
            for index, payload in enumerate((b"corrupt!", b"short")):
                destination = root / str(index)
                with self.assertRaises(ValueError):
                    heavy_run.receive_result(io.BytesIO(header + payload), io.BytesIO(), destination, reference)
                self.assertEqual((reference / "file").read_bytes(), b"original")
                self.assertTrue(destination.exists())
                self.assertTrue((destination / "file").exists())

    def test_collection_rejects_escaping_inventory_and_existing_destination_before_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            original = root / "original"
            original.mkdir()
            (original / "file").write_bytes(b"original")
            expected = heavy_run.result_inventory(original)
            for name in ("..", "a/.."):
                inventory = {**expected, "a": expected["."], name: expected["file"]}
                with self.assertRaises(ValueError):
                    heavy_run.receive_result(io.BytesIO(json.dumps(inventory).encode() + b"\n"), io.BytesIO(), root / "new")
                self.assertFalse((root / "new").exists())
            with self.assertRaises(FileExistsError):
                heavy_run.receive_result(io.BytesIO(json.dumps(expected).encode() + b"\n"), io.BytesIO(), original)
            self.assertEqual((original / "file").read_bytes(), b"original")

    def test_unsupported_clones_transfer_instead_and_storage_failures_preserve_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            reference = root / "reference"
            reference.write_bytes(b"same bytes")
            inventory = heavy_run.result_inventory(reference)
            header = json.dumps(inventory).encode() + b"\n"
            for code in (errno.EXDEV, errno.ENOTSUP, errno.ENOSPC):
                destination = root / str(code)
                with patch.object(heavy_run, "clone_input", side_effect=OSError(code, "fixture")):
                    if code == errno.ENOSPC:
                        with self.assertRaises(OSError):
                            heavy_run.receive_result(
                                io.BytesIO(header + reference.read_bytes()), io.BytesIO(), destination, reference
                            )
                    else:
                        report = heavy_run.receive_result(
                            io.BytesIO(header + reference.read_bytes()), io.BytesIO(), destination, reference
                        )
                        self.assertEqual(report["cloned_files"], 0)
                        self.assertEqual(report["transferred_bytes"], reference.stat().st_size)
                        self.assertEqual(destination.read_bytes(), reference.read_bytes())
            self.assertEqual(reference.read_bytes(), b"same bytes")


class InputStagingTests(unittest.TestCase):
    @staticmethod
    def docker(evidence: Path, staging: str | None = None) -> heavy_run.Docker:
        return heavy_run.Docker(
            heavy_run.Slot(0, io.BytesIO(), "fixture"),
            io.StringIO(),
            "unix:///fixture",
            evidence,
            staging or heavy_run.STAGING_PREFIX + uuid.uuid4().hex,
        )

    @unittest.skipUnless(sys.platform == "darwin", "Native clone fixture requires macOS")
    def test_locked_clones_and_read_only_workspace_collect_without_changing_original_flags_or_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "original"
            source.mkdir()
            payload = source / "payload"
            payload.write_bytes(os.urandom(1024 * 1024))
            payload.chmod(0o400)
            expected = payload.read_bytes()
            evidence = root / "evidence"
            evidence.mkdir()
            (evidence / "manifest.json").write_text("caller manifest")
            hashes = {"payload": hashlib.sha256(expected).hexdigest()}
            (evidence / "hashes.json").write_text(json.dumps(hashes))
            (evidence / "output").write_bytes(b"collected output")
            os.chflags(payload, stat.UF_IMMUTABLE)
            original_metadata = payload.stat()
            try:
                direct = Path(temporary) / "direct-clone"
                heavy_run.clone_input(Path(temporary) / "original" / "payload", direct)
                self.assertEqual(direct.read_bytes(), expected)
                owner = self.docker(evidence)
                staged = owner.stage_input(source)
                image = owner.stage_input(payload)
                self.assertNotEqual((staged / "payload").stat().st_ino, payload.stat().st_ino)
                heavy_run.Docker.stage_overlay_input(staged)
                (staged / "payload").write_bytes(b"independent workspace write")
                readonly = staged / "readonly"
                readonly.mkdir(mode=0o555)
                os.chflags(staged / "payload", stat.UF_IMMUTABLE)
                owner.collect_staged_inputs()
                self.assertFalse(staged.exists())
                self.assertFalse(image.exists())
                self.assertEqual(payload.read_bytes(), expected)
                self.assertEqual(payload.stat().st_flags, original_metadata.st_flags)
                self.assertEqual(payload.stat().st_mode, original_metadata.st_mode)
                self.assertEqual(json.loads((evidence / "hashes.json").read_text()), hashes)
                self.assertEqual((evidence / "manifest.json").read_text(), "caller manifest")
                self.assertEqual((evidence / "output").read_bytes(), b"collected output")
            finally:
                os.chflags(payload, 0)

    def test_same_device_never_uses_the_copy_route_and_linux_clone_failure_preserves_original(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "original"
            source.write_bytes(b"original")
            evidence = root / "evidence"
            evidence.mkdir()
            owner = self.docker(evidence)
            with patch.object(heavy_run, "clone_input", side_effect=shutil.copyfile) as clone:
                staged = owner.stage_input(source)
            clone.assert_called_once_with(source, staged)
            self.assertEqual(owner.storage["copied_bytes"], 0)
            owner.collect_staged_inputs()
            with (
                patch.object(sys, "platform", "linux"),
                patch.object(heavy_run.fcntl, "FICLONE", 123, create=True),
                patch.object(heavy_run.fcntl, "ioctl", side_effect=OSError(errno.ENOTSUP, "fixture")),
            ):
                with self.assertRaises(OSError):
                    heavy_run.clone_input(source, root / "failed-clone")
            self.assertFalse((root / "failed-clone").exists())
            self.assertEqual(source.read_bytes(), b"original")
            original_stat = Path.stat
            metadata = source.stat()
            foreign_metadata = os.stat_result((*metadata[:2], metadata.st_dev + 1, *metadata[3:]))

            def other_device(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
                return foreign_metadata if path == source else original_stat(path, follow_symlinks=follow_symlinks)

            other = self.docker(evidence)
            with (
                patch.object(Path, "stat", autospec=True, side_effect=other_device),
                patch.object(heavy_run, "clone_input", side_effect=AssertionError("Cross-device inputs must copy")),
            ):
                copied = other.stage_input(source)
            self.assertEqual(copied.read_bytes(), source.read_bytes())
            self.assertEqual(other.storage["copied_bytes"], source.stat().st_size)
            other.collect_staged_inputs()

    def test_prepare_failures_and_crash_recovery_collect_only_registered_staging(self) -> None:
        def prepare(driver: heavy_run.Docker, original: Path, action: str, requested: dict) -> dict:
            prepared_input = driver.stage_input(original)
            (prepared_input.parent / "readonly").mkdir(mode=0o555)
            if action == "canceled":
                raise heavy_run.Canceled(signal.SIGTERM)
            return requested

        for failure in ("unsupported", "canceled", "output", "crash"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                source = root / "original"
                source.write_bytes(b"preserve original")
                evidence = root / "evidence"
                manifest = {"containers": [{"name": "job", "image": "fixture"}], "job": "job"}
                clone = OSError(errno.ENOTSUP, "fixture lacks clones") if failure == "unsupported" else shutil.copyfile
                with (
                    patch.object(heavy_run, "clone_input", side_effect=clone),
                    patch.object(heavy_run, "local_endpoint", return_value="unix:///fixture"),
                ):
                    with (
                        patch.object(heavy_run.Docker, "call", return_value=""),
                        patch.object(heavy_run.Docker, "run", return_value=(23, {})),
                    ):
                        if failure == "crash":
                            evidence.mkdir()
                            owner = self.docker(evidence)
                            staged = owner.stage_input(source)
                            custody = root / "custody"
                            heavy_run.Custody(owner.endpoint, evidence, owner.staging).save(custody)
                            unowned = root / (heavy_run.STAGING_PREFIX + uuid.uuid4().hex)
                            unowned.mkdir()
                            (unowned / ".owner").write_text(owner.label)
                            (unowned / "payload").write_bytes(b"keep")
                            heavy_run.recover(owner.slot, custody, io.StringIO(), owner.endpoint)
                            self.assertFalse(custody.exists())
                            self.assertFalse(staged.exists())
                            self.assertEqual((unowned / "payload").read_bytes(), b"keep")
                            custody.write_text(json.dumps({"endpoint": owner.endpoint, "evidence": str(evidence)}))
                            heavy_run.recover(owner.slot, custody, io.StringIO(), owner.endpoint)
                            self.assertEqual((unowned / "payload").read_bytes(), b"keep")
                        else:
                            with patch.object(heavy_run.Docker, "copy_outputs", side_effect=OSError("fixture missing output")):
                                result = heavy_run.run(
                                    {},
                                    evidence,
                                    heavy_run.Settings(),
                                    root,
                                    prepare=partial(prepare, original=source, action=failure, requested=manifest),
                                )
                                self.assertNotEqual(result, 0)
                            receipt = json.loads((evidence / "result.json").read_text())
                            self.assertTrue(receipt["cleanup_complete"])
                            self.assertEqual(receipt["storage"]["cloned_files"], 0 if failure == "unsupported" else 1)
                if failure != "crash":
                    self.assertFalse([path for path in root.iterdir() if path.name.startswith(heavy_run.STAGING_PREFIX)])
                self.assertEqual(source.read_bytes(), b"preserve original")

    def test_foreign_staging_is_preserved_without_holding_a_clean_runtime_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "original"
            source.write_bytes(b"keep original")
            evidence = root / "evidence"
            retained = []
            manifest = {"containers": [{"name": "job", "image": "fixture"}], "job": "job"}

            def prepare(driver: heavy_run.Docker) -> dict:
                staged = driver.stage_input(source)
                retained.append(staged)
                (staged.parent / ".owner").write_text("foreign owner")
                return manifest

            with (
                patch.object(heavy_run, "clone_input", side_effect=shutil.copyfile),
                patch.object(heavy_run, "local_endpoint", return_value="unix:///fixture"),
            ):
                with (
                    patch.object(heavy_run.Docker, "call", return_value=""),
                    patch.object(heavy_run.Docker, "run", return_value=(0, {})),
                ):
                    self.assertNotEqual(heavy_run.run({}, evidence, heavy_run.Settings(), root, prepare=prepare), 0)
                    receipt = json.loads((evidence / "result.json").read_text())
                    self.assertTrue(receipt["cleanup_complete"])
                    self.assertIn("staging_cleanup_error", receipt)
                    self.assertEqual(heavy_run.run(manifest, root / "successor", heavy_run.Settings(), root), 0)
            self.assertEqual(retained[0].read_bytes(), b"keep original")
            self.assertEqual(source.read_bytes(), b"keep original")
            self.assertFalse(list((root / heavy_run.STATE_PATH).glob("custody-*")))
            marker = retained[0].parent / ".owner"
            marker.unlink()
            os.mkfifo(marker)
            entry = (
                "import io,sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import heavy_run; "
                "owner=heavy_run.Docker(heavy_run.Slot(0,io.BytesIO(),'fixture'),io.StringIO(),'unix:///fixture',Path(sys.argv[2]),sys.argv[3]); "
                "assert owner.finish_staging(recovery=True) is not None"
            )
            bounded = subprocess.run(
                [sys.executable, "-c", entry, str(Path(__file__).parent), str(evidence), retained[0].parent.name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(bounded.returncode, 0, bounded.stderr)
            self.assertEqual(source.read_bytes(), b"keep original")
            held = root / "held-staging"
            marker.parent.rename(held)
            marker.parent.symlink_to(source)
            owner = self.docker(evidence, marker.parent.name)
            self.assertIsNotNone(owner.finish_staging(recovery=True))
            self.assertEqual(source.read_bytes(), b"keep original")
            self.assertTrue((held / retained[0].name).exists())

    def test_runtime_cleanup_failure_keeps_staging_and_reports_its_storage_cost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "original"
            source.write_bytes(b"preserve original")
            evidence = root / "evidence"
            manifest = {"containers": [{"name": "job", "image": "fixture"}], "job": "job"}

            def prepare(owner: heavy_run.Docker) -> dict:
                owner.stage_input(source)
                return manifest

            with (
                patch.object(heavy_run, "clone_input", side_effect=shutil.copyfile),
                patch.object(heavy_run, "recover"),
                patch.object(heavy_run, "local_endpoint", return_value="unix:///fixture"),
            ):
                with (
                    patch.object(heavy_run.Docker, "run", return_value=(0, {})),
                    patch.object(heavy_run.Docker, "cleanup", side_effect=RuntimeError("fixture cleanup failure")),
                ):
                    self.assertNotEqual(heavy_run.run({}, evidence, heavy_run.Settings(), root, prepare=prepare), 0)
            receipt = json.loads((evidence / "result.json").read_text())
            self.assertFalse(receipt["cleanup_complete"])
            self.assertEqual(receipt["storage"]["cloned_files"], 1)
            self.assertTrue(list((root / heavy_run.STATE_PATH).glob("custody-*")))
            self.assertTrue([path for path in root.iterdir() if path.name.startswith(heavy_run.STAGING_PREFIX)])
            self.assertEqual(source.read_bytes(), b"preserve original")

    def test_stream_only_run_does_not_require_read_access_to_staging_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            original_open = os.open
            metadata_only = getattr(os, "O_PATH", 0) | getattr(os, "O_EVTONLY", 0)

            def denied(path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
                if Path(path) == root and not flags & metadata_only:
                    raise PermissionError(errno.EACCES, "Fixture parent is traversable but not readable")
                return original_open(path, flags, mode, dir_fd=dir_fd)

            manifest = {"containers": [{"name": "job", "image": "fixture"}], "job": "job"}
            with (
                patch.object(os, "open", side_effect=denied),
                patch.object(heavy_run, "local_endpoint", return_value="unix:///fixture"),
            ):
                with (
                    patch.object(heavy_run.Docker, "call", return_value=""),
                    patch.object(heavy_run.Docker, "run", return_value=(0, {})),
                ):
                    self.assertEqual(heavy_run.run(manifest, root / "run", heavy_run.Settings(), root), 0)

    @unittest.skipUnless(os.environ.get("HEAVY_STAGING_FIXTURE_VOLUME"), "Requires an isolated fixture volume")
    def test_fixture_volume_clones_without_full_allocation_and_copies_across_devices(self) -> None:
        volume = Path(os.environ["HEAVY_STAGING_FIXTURE_VOLUME"])
        with tempfile.TemporaryDirectory(dir=volume) as temporary, tempfile.TemporaryDirectory() as outside:
            root = Path(temporary).resolve()
            source = root / "original"
            source.write_bytes(os.urandom(32 * 1024 * 1024))
            expected = source.read_bytes()
            evidence = root / "evidence"
            evidence.mkdir()
            subprocess.run(["sync"], check=True)
            owner = self.docker(evidence)
            staged = owner.stage_input(source)
            subprocess.run(["sync"], check=True)
            self.assertLess(owner.free_before - shutil.disk_usage(volume).free, source.stat().st_size // 2)
            self.assertEqual(staged.read_bytes(), expected)
            custody = root / "custody"
            heavy_run.Custody(owner.endpoint, evidence, owner.staging).save(custody)
            owner = self.docker(evidence, heavy_run.Custody.load(custody).staging)
            owner.collect_staged_inputs()
            self.assertFalse(staged.exists())
            external = Path(outside).resolve() / "evidence"
            external.mkdir()
            self.assertNotEqual(source.stat().st_dev, external.stat().st_dev)
            other = self.docker(external)
            with patch.object(heavy_run, "clone_input", side_effect=AssertionError("Cross-volume inputs must copy")):
                copied = other.stage_input(source)
            self.assertEqual(other.storage["copied_bytes"], len(expected))
            self.assertEqual(copied.read_bytes(), expected)
            copied.write_bytes(b"independent cross-volume copy")
            self.assertEqual(source.read_bytes(), expected)
            other.collect_staged_inputs()


class WorkloadExitTests(unittest.TestCase):
    def test_job_creation_keeps_image_bootstrap_with_default_and_overridden_command(self) -> None:
        docker = heavy_run.Docker(heavy_run.Slot(0, io.BytesIO(), "fixture"), io.StringIO(), "unix:///fixture")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bootstrap = root / "bootstrap.py"
            bootstrap.write_text('import os,sys; os.environ["BOOTSTRAPPED"]="yes"; os.execvp(sys.argv[1], sys.argv[1:])')
            default = [sys.executable, "-c", 'import os; assert os.environ["BOOTSTRAPPED"]=="yes"; raise SystemExit(255)']
            config = {"Entrypoint": [sys.executable, str(bootstrap)], "Cmd": default}
            for command in ([], [sys.executable, "-c", 'import os; assert os.environ["BOOTSTRAPPED"]=="yes"; raise SystemExit(23)']):
                with self.subTest(command=command), patch.object(docker, "call", side_effect=[json.dumps(config), "job"]) as call:
                    docker.create({"name": "job", "image": "fixture", "command": command}, "run", "1", "1024", root, workload=True)
                    arguments = call.call_args.args
                    result = subprocess.run([sys.executable, *arguments[arguments.index("fixture") + 1 :]], timeout=10)
                    self.assertEqual(result.returncode, 23 if command else 127)

    def test_actual_child_exits_preserve_success_and_normalize_high_failures(self) -> None:
        for status in (0, 23, 127, 128, 255):
            with self.subTest(status=status):
                result = subprocess.run(
                    [sys.executable, "-c", heavy_run.WORKLOAD_ENTRYPOINT, sys.executable, "-c", f"raise SystemExit({status})"],
                    capture_output=True,
                    timeout=10,
                )
                if status < 128:
                    self.assertEqual(result.returncode, status)
                else:
                    self.assertIn(result.returncode, range(1, 128))

    def test_child_signal_and_missing_executable_are_not_ordinary_exits(self) -> None:
        child = "import os,signal; signal.signal(signal.SIGTERM, signal.SIG_DFL); os.kill(os.getpid(), signal.SIGTERM)"
        signaled = subprocess.run([sys.executable, "-c", heavy_run.WORKLOAD_ENTRYPOINT, sys.executable, "-c", child], timeout=10)
        self.assertEqual(signaled.returncode, 128 + signal.SIGTERM)
        missing = subprocess.run(
            [sys.executable, "-c", heavy_run.WORKLOAD_ENTRYPOINT, "/no-such-heavy-workload"], capture_output=True, timeout=10
        )
        self.assertEqual(missing.returncode, 255)

    def test_bootstrap_argv_environment_and_streams_reach_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bootstrap = Path(directory) / "bootstrap.py"
            bootstrap.write_text('import os,sys; os.environ["BOOTSTRAPPED"]="yes"; os.execvp(sys.argv[1], sys.argv[1:])')
            child = 'import json,os,sys; print(json.dumps([sys.argv[1:],os.environ["BOOTSTRAPPED"],os.environ["VALUE"],sys.stdin.read()])); print("stderr",file=sys.stderr)'
            arguments = ["space value", "", "$(exit 255)"]
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    heavy_run.WORKLOAD_ENTRYPOINT,
                    sys.executable,
                    str(bootstrap),
                    sys.executable,
                    "-c",
                    child,
                    *arguments,
                ],
                env={**os.environ, "VALUE": "inherited"},
                input="stdin",
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), [arguments, "yes", "inherited", "stdin"])
            self.assertEqual(result.stderr, "stderr\n")

    def test_forwarded_cancellation_cannot_become_success(self) -> None:
        child = 'import signal,time; signal.signal(signal.SIGTERM,lambda *_:exit(0)); print("ready",flush=True); time.sleep(30)'
        with subprocess.Popen(
            [sys.executable, "-c", heavy_run.WORKLOAD_ENTRYPOINT, sys.executable, "-c", child], stdout=subprocess.PIPE, text=True
        ) as process:
            try:
                assert process.stdout is not None
                self.assertEqual(process.stdout.readline(), "ready\n")
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=10), 128 + signal.SIGTERM)
            finally:
                if process.poll() is None:
                    process.kill()


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
            self.volumes[name] = {"Name": name, "Driver": "local", "Options": options, "Labels": labels, "Mountpoint": str(backing)}
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
        for mutation in ("bytes", "empty_directory", "mode"):
            with self.subTest(mutation=mutation):
                self.docker.observe_overlay_lower(self.lower)
                attachment = self.lower / "attachment"
                if mutation == "bytes":
                    attachment.write_bytes(b"changed before entry")
                elif mutation == "empty_directory":
                    (self.lower / "new-directory").mkdir()
                    (self.lower / "new-directory").chmod(0o777)
                else:
                    attachment.chmod(0o777)
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

    @staticmethod
    def changed_stat(*, ino: int = 0, dev: int = 0, uid: int = 0) -> AbstractContextManager[object]:
        original = Path.lstat

        def lstat(path: Path) -> os.stat_result:
            values = list(original(path)[:10])
            values[1] += ino
            values[2] += dev
            values[4] += uid
            return os.stat_result(values)

        return patch.object(Path, "lstat", autospec=True, side_effect=lstat)

    def create_labeled_overlay(self, lower: Path) -> None:
        with patch.object(os, "uname", return_value=type("System", (), {"sysname": "Linux"})()):
            self.docker.create_overlay({**self.copy, "source": str(lower)}, "owned")

    def test_device_and_inode_change_with_unchanged_content_clears_custody(self) -> None:
        self.create_labeled_overlay(self.lower)
        with self.changed_stat(ino=1000, dev=1):
            self.docker.cleanup_overlay_volumes()
        self.assertFalse(self.docker.volumes)

    def test_device_change_does_not_hide_changed_content(self) -> None:
        self.create_labeled_overlay(self.lower)
        (self.lower / "attachment").write_bytes(b"unexpected host change")
        with self.changed_stat(ino=1000, dev=1), self.assertRaises(RuntimeError):
            self.docker.cleanup_overlay_volumes()
        self.assertEqual(len(self.docker.volumes), 2)

    def retained_lower(self) -> tuple[Path, Path, dict[str, str]]:
        job = self.root / "jobs" / "original"
        lower = job / "inputs" / "3"
        (lower / "nested").mkdir(parents=True)
        (lower / "attachment").write_bytes(b"original attachment")
        (lower / "nested" / "other").write_bytes(b"other")
        heavy_run.Docker.stage_overlay_input(lower)
        recorded = {
            "inputs/3/attachment": heavy_run.hashlib.sha256(b"original attachment").hexdigest(),
            "inputs/3/nested/other": heavy_run.hashlib.sha256(b"other").hexdigest(),
            "images/image.tar": "0" * 64,
        }
        (job / "complete.json").write_text(json.dumps({"hashes": recorded, "images": []}))
        return lower, job / "complete.json", recorded

    def relabel_legacy(self, value: str = "a" * 64) -> None:
        labels = self.docker.volumes["owned"]["Labels"]
        labels[heavy_run.LOWER_DIGEST_LABEL] = value

    def test_legacy_label_clears_only_with_exact_complete_file_evidence(self) -> None:
        lower, _inventory, _recorded = self.retained_lower()
        self.create_labeled_overlay(lower)
        self.relabel_legacy()
        with self.changed_stat(ino=1000, dev=1):
            self.docker.cleanup_overlay_volumes()
        self.assertFalse(self.docker.volumes)
        self.assertEqual((lower / "attachment").read_bytes(), b"original attachment")

    def test_legacy_label_without_complete_proof_keeps_custody(self) -> None:
        def missing(_lower: Path, inventory: Path, _recorded: dict[str, str]) -> None:
            inventory.unlink()

        def malformed(_lower: Path, inventory: Path, _recorded: dict[str, str]) -> None:
            inventory.write_text("{")

        def hashes_not_a_map(_lower: Path, inventory: Path, _recorded: dict[str, str]) -> None:
            inventory.write_text(json.dumps({"hashes": []}))

        def no_entries_for_this_input(_lower: Path, inventory: Path, recorded: dict[str, str]) -> None:
            inventory.write_text(
                json.dumps({"hashes": {key: value for key, value in recorded.items() if key.startswith("images/")}})
            )

        def partial_inventory(_lower: Path, inventory: Path, recorded: dict[str, str]) -> None:
            del recorded["inputs/3/attachment"]
            inventory.write_text(json.dumps({"hashes": recorded}))

        def recorded_file_removed(lower: Path, _inventory: Path, _recorded: dict[str, str]) -> None:
            (lower / "nested" / "other").unlink()

        def changed_bytes(lower: Path, _inventory: Path, _recorded: dict[str, str]) -> None:
            (lower / "attachment").write_bytes(b"unexpected host change")

        def made_executable(lower: Path, _inventory: Path, _recorded: dict[str, str]) -> None:
            (lower / "attachment").chmod(0o777)

        def added_empty_directory(lower: Path, _inventory: Path, _recorded: dict[str, str]) -> None:
            (lower / "unrecorded").mkdir()
            (lower / "unrecorded").chmod(0o777)

        def aliased_inventory(_lower: Path, inventory: Path, _recorded: dict[str, str]) -> None:
            target = inventory.with_name("elsewhere.json")
            inventory.replace(target)
            inventory.symlink_to(target)

        for damage in (
            missing,
            malformed,
            hashes_not_a_map,
            no_entries_for_this_input,
            partial_inventory,
            recorded_file_removed,
            changed_bytes,
            made_executable,
            added_empty_directory,
            aliased_inventory,
        ):
            with self.subTest(damage=damage.__name__):
                staged = self.retained_lower()
                self.create_labeled_overlay(staged[0])
                self.relabel_legacy()
                damage(*staged)
                with self.assertRaises(RuntimeError):
                    self.docker.cleanup_overlay_volumes()
                self.assertEqual(len(self.docker.volumes), 2)
                self.docker.volumes = {}
                shutil.rmtree(self.root / "jobs")
                shutil.rmtree(self.docker.root / "volumes")

    def test_legacy_label_refuses_a_lower_owned_by_someone_else(self) -> None:
        lower, _inventory, _recorded = self.retained_lower()
        self.create_labeled_overlay(lower)
        self.relabel_legacy()
        with self.changed_stat(uid=1), self.assertRaises(RuntimeError):
            self.docker.cleanup_overlay_volumes()
        self.assertEqual(len(self.docker.volumes), 2)

    def test_legacy_proof_needs_the_job_input_layout(self) -> None:
        self.create_labeled_overlay(self.lower)
        self.relabel_legacy()
        with self.assertRaises(RuntimeError):
            self.docker.cleanup_overlay_volumes()
        self.assertEqual(len(self.docker.volumes), 2)

    def test_content_label_never_falls_back_to_legacy_evidence(self) -> None:
        lower, _inventory, _recorded = self.retained_lower()
        self.create_labeled_overlay(lower)
        self.relabel_legacy(heavy_run.LOWER_DIGEST_PREFIX + "a" * 64)
        with self.assertRaises(RuntimeError):
            self.docker.cleanup_overlay_volumes()
        self.assertEqual(len(self.docker.volumes), 2)

    def test_unrecognized_label_format_keeps_custody(self) -> None:
        lower, _inventory, _recorded = self.retained_lower()
        self.create_labeled_overlay(lower)
        self.relabel_legacy("not-a-recognized-digest")
        with self.assertRaises(RuntimeError):
            self.docker.cleanup_overlay_volumes()
        self.assertEqual(len(self.docker.volumes), 2)

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
        if not (source / "private-link").exists():
            (source / "private-link").symlink_to("private.txt")
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
        for expected in (23, 255):
            with self.subTest(expected=expected):
                name = f"failed-job-{expected}"
                process = self.launch(name, job_exit=expected, fail_output=True)
                wait_for(self.root / f"docker/started-{name}")
                (self.root / f"docker/release-{name}").touch()
                process.communicate(timeout=15)
                self.assertEqual(process.returncode, expected)
                result = json.loads((self.root / f"{name}/result.json").read_text())
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
