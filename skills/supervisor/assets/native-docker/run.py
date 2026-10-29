from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def vm_limits(cgroup: Path) -> tuple[int, int]:
    memory = int((cgroup / "memory.max").read_text())
    quota, period = (cgroup / "cpu.max").read_text().split()
    memory_mib = memory * 3 // (4 * 1024**2)
    if memory_mib < 512:
        raise ValueError("Native Docker VM needs at least 512 MiB after reserving outer-job memory")
    return memory_mib, max(1, int(quota) // int(period))


def run_vm(workspace: Path, cgroup: Path, command: list[str], environment: dict[str, str]) -> int:
    control = workspace / ".heavy-vm"
    control.mkdir(mode=0o700)
    (control / "request.json").write_text(json.dumps({"command": command, "environment": environment}))
    memory, cpus = vm_limits(cgroup)
    disk_gib = int(environment.get("HEAVY_VM_DISK_GIB", "16"))
    timeout = int(environment.get("HEAVY_VM_TIMEOUT_SECONDS", "3600"))
    if disk_gib < 1 or timeout < 1:
        raise ValueError("VM disk GiB and timeout seconds must be positive")
    disk = control / "data.raw"
    with disk.open("wb") as stream:
        stream.truncate(disk_gib * 1024**3)
    subprocess.run(["mkfs.ext4", "-q", str(disk)], check=True)
    result = subprocess.run(
        [
            "qemu-system-x86_64",
            "-nodefaults",
            "-machine",
            "q35",
            "-accel",
            "tcg,thread=multi",
            "-cpu",
            "max",
            "-smp",
            str(cpus),
            "-m",
            str(memory),
            "-display",
            "none",
            "-serial",
            "stdio",
            "-monitor",
            "none",
            "-no-reboot",
            "-nic",
            "none",
            "-kernel",
            "/native/vmlinuz",
            "-initrd",
            "/native/initramfs.gz",
            "-append",
            "console=ttyS0 panic=1 rdinit=/init quiet",
            "-drive",
            f"file={disk},if=virtio,format=raw",
            "-virtfs",
            "local,path=/workspace,mount_tag=workspace,security_model=none",
        ],
        timeout=timeout,
    )
    if result.returncode:
        return 1
    return int((control / "exit-code").read_text())


if __name__ == "__main__":
    raise SystemExit(run_vm(Path("/workspace"), Path("/sys/fs/cgroup"), sys.argv[1:], dict(os.environ)))
