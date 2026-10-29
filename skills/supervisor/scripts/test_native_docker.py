#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location("native_docker", Path(__file__).parents[1] / "assets/native-docker/run.py")
assert SPEC is not None and SPEC.loader is not None
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)
GUEST_SPEC = importlib.util.spec_from_file_location("native_guest", Path(__file__).parents[1] / "assets/native-docker/guest.py")
assert GUEST_SPEC is not None and GUEST_SPEC.loader is not None
guest = importlib.util.module_from_spec(GUEST_SPEC)
GUEST_SPEC.loader.exec_module(guest)


class NativeDockerTests(unittest.TestCase):
    def test_readiness_timeout_can_recover_but_daemon_loss_fails(self) -> None:
        daemon = Mock()
        daemon.poll.return_value = None
        with patch.object(
            subprocess, "run", side_effect=[subprocess.TimeoutExpired("docker", 1), subprocess.CompletedProcess([], 0)]
        ):
            guest.wait_for_docker(daemon, {})
        daemon.poll.return_value = 1
        with patch.object(subprocess, "run") as execute:
            with self.assertRaises(RuntimeError):
                guest.wait_for_docker(daemon, {})
            execute.assert_not_called()

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cgroup = self.root / "cgroup"
        self.cgroup.mkdir()
        (self.cgroup / "memory.max").write_text(str(4 * 1024**3))
        (self.cgroup / "cpu.max").write_text("200000 100000")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def test_unbounded_or_insufficient_resources_refuse_execution(self) -> None:
        for file, value in (("memory.max", "max"), ("memory.max", "1"), ("cpu.max", "max 100000")):
            with self.subTest(file=file, value=value):
                path = self.cgroup / file
                original = path.read_text()
                path.write_text(value)
                try:
                    with self.assertRaises(ValueError):
                        native.vm_limits(self.cgroup)
                finally:
                    path.write_text(original)

    def test_guest_completion_preserves_failure_and_requires_a_terminal_status(self) -> None:
        for status in (None, 0, 23):
            with self.subTest(status=status), tempfile.TemporaryDirectory(dir=self.workspace) as temporary:
                workspace = Path(temporary)

                def execute(
                    argv: list[str],
                    *,
                    test_status: int | None = status,
                    test_workspace: Path = workspace / "state",
                    **_kwargs: object,
                ) -> subprocess.CompletedProcess[str]:
                    if test_status is not None:
                        (test_workspace / "control/exit-code").write_text(str(test_status))
                    return subprocess.CompletedProcess(argv, 0)

                with patch.object(subprocess, "run", side_effect=execute):
                    if status is None:
                        with self.assertRaises(FileNotFoundError):
                            native.run_vm(workspace / "state", self.cgroup, ["fixture"], {"HEAVY_VM_DISK_GIB": "1"})
                    else:
                        self.assertEqual(
                            native.run_vm(workspace / "state", self.cgroup, ["fixture"], {"HEAVY_VM_DISK_GIB": "1"}), status
                        )

    def test_vm_failure_or_timeout_cannot_report_success(self) -> None:
        for failure in (subprocess.CompletedProcess([], 1), subprocess.TimeoutExpired("qemu", 1)):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(dir=self.workspace) as temporary:
                with patch.object(subprocess, "run", side_effect=[subprocess.CompletedProcess([], 0), failure]):
                    if isinstance(failure, subprocess.TimeoutExpired):
                        with self.assertRaises(subprocess.TimeoutExpired):
                            native.run_vm(Path(temporary) / "state", self.cgroup, ["fixture"], {"HEAVY_VM_DISK_GIB": "1"})
                    else:
                        self.assertNotEqual(
                            native.run_vm(Path(temporary) / "state", self.cgroup, ["fixture"], {"HEAVY_VM_DISK_GIB": "1"}), 0
                        )


if __name__ == "__main__":
    unittest.main()
