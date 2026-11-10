#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import BinaryIO, TextIO

LABEL = "org.codex-skills.heavy-slot"
LOWER_DIGEST_LABEL = "org.codex-skills.heavy-lower-sha256"
LOWER_DIGEST_PREFIX = "content-v1:"
LEGACY_LOWER_DIGEST = re.compile(r"[0-9a-f]{64}")
CONFIG_PATH = Path(".config/codex-skills/heavy-run.json")
STATE_PATH = Path(".local/state/codex-skills/heavy-run")
MIN_NANO_CPUS = 10**7
MIN_MEMORY = 6 * 1024**2
STAGING_PREFIX = ".heavy-inputs-"
CLONE_NOFOLLOW = 1
CLONE_NOOWNERCOPY = 2
CLONE_NOFOLLOW_ANY = 8
USER_FLAGS = getattr(stat, "UF_IMMUTABLE", 0) | getattr(stat, "UF_APPEND", 0)
SYSTEM_FLAGS = getattr(stat, "SF_IMMUTABLE", 0) | getattr(stat, "SF_APPEND", 0)
CANCEL_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
WORKLOAD_ENTRYPOINT = """import signal, subprocess, sys
canceled = 0
try:
    child = subprocess.Popen(sys.argv[1:])
    def forward(signum: int, _frame: object) -> None:
        global canceled
        canceled = signum
        child.send_signal(signum)
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, forward)
    status = child.wait()
except (OSError, ValueError):
    sys.exit(255)
sys.exit(128 + canceled if canceled else 128 - status if status < 0 else status if status < 128 else 127)
"""


def clear_staged_flags(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if sys.platform != "darwin" or not metadata.st_flags & USER_FLAGS:
        return
    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
        raise RuntimeError("Linked staging cannot change an original's flags")
    change = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True).fchflags
    change.argtypes = [ctypes.c_int, ctypes.c_uint]
    change.restype = ctypes.c_int
    if change(descriptor, metadata.st_flags & ~USER_FLAGS):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def clone_input(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError("Clone an original regular file; use manifest references for links")
    source = source.resolve(strict=True)
    destination = destination.parent.resolve(strict=True) / destination.name
    if sys.platform == "darwin":
        if source.stat().st_flags & SYSTEM_FLAGS:
            raise OSError(errno.ENOTSUP, "Stream system-locked originals through the manifest instead")
        clone = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True).clonefile
        clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        clone.restype = ctypes.c_int
        if clone(os.fsencode(source), os.fsencode(destination), CLONE_NOFOLLOW | CLONE_NOOWNERCOPY | CLONE_NOFOLLOW_ANY):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(destination))
        descriptor = os.open(destination, os.O_EVTONLY | os.O_NOFOLLOW)
        try:
            clear_staged_flags(descriptor)
        finally:
            os.close(descriptor)
        return
    if sys.platform == "linux":
        with source.open("rb") as original, destination.open("xb") as target:
            try:
                fcntl.ioctl(target.fileno(), fcntl.FICLONE, original.fileno())
            except OSError:
                destination.unlink()
                raise
        return
    raise OSError(errno.ENOTSUP, "Use the original manifest source for read-only streaming on this host")


def input_entries(root: Path) -> Generator[Path, None, None]:
    for path in [root, *sorted(root.rglob("*"))] if root.is_dir() else [root]:
        if not stat.S_ISDIR(path.lstat().st_mode) and not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("Inputs must contain only ordinary files and directories")
        yield path


def result_inventory(root: Path) -> dict[str, dict]:
    inventory: dict[str, dict] = {}
    for path in input_entries(root):
        before = path.stat()
        entry: dict[str, int | str] = {"mode": stat.S_IMODE(before.st_mode)}
        if path.is_file():
            with path.open("rb") as stream:
                entry.update(size=before.st_size, sha256=hashlib.file_digest(stream, "sha256").hexdigest())
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("Result changed during inventory; preserve evidence")
        inventory[str(path.relative_to(root))] = entry
    return inventory


def validate_result_inventory(inventory: object) -> None:
    if not isinstance(inventory, dict) or "." not in inventory:
        raise ValueError("Result inventory must include its root")
    for name, entry in inventory.items():
        if not isinstance(name, str) or str(Path(name)) != name or Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("Result inventory paths must be canonical and relative")
        if (
            not isinstance(entry, dict)
            or set(entry) not in ({"mode"}, {"mode", "size", "sha256"})
            or type(entry.get("mode")) is not int
            or not 0 <= entry["mode"] <= 0o777
        ):
            raise ValueError("Invalid result inventory entry")
        if "sha256" in entry and (
            type(entry["size"]) is not int
            or entry["size"] < 0
            or not isinstance(entry["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        ):
            raise ValueError("Invalid result size or hash")
        if name != "." and any(set(inventory.get(str(parent), {})) != {"mode"} for parent in Path(name).parents):
            raise ValueError("Result parents must be recorded directories")


def transfer_result_bytes(source: BinaryIO, destination: BinaryIO, size: int) -> None:
    remaining = size
    while remaining:
        chunk = source.read(min(remaining, 1024 * 1024))
        if not chunk:
            raise ValueError("Result transfer interrupted; preserve partial evidence")
        pending = memoryview(chunk)
        while pending:
            written = destination.write(pending)
            if written is None or written <= 0:
                raise OSError("Result transport stopped accepting bytes")
            pending = pending[written:]
        remaining -= len(chunk)


def send_result(root: Path, stream: BinaryIO, requests: BinaryIO) -> None:
    inventory = result_inventory(root)
    validate_result_inventory(inventory)
    stream.write(json.dumps(inventory).encode() + b"\n")
    stream.flush()
    selected = json.loads(requests.readline())
    if (
        not isinstance(selected, list)
        or any(not isinstance(name, str) or name not in inventory or "sha256" not in inventory[name] for name in selected)
        or len(set(selected)) != len(selected)
    ):
        raise ValueError("Request only unique recorded result files")
    for name in selected:
        with (root if name == "." else root / name).open("rb") as source:
            transfer_result_bytes(source, stream, inventory[name]["size"])
    stream.flush()


def receive_result(stream: BinaryIO, requests: BinaryIO, destination: Path, reference: Path | None = None) -> dict[str, int]:
    inventory: dict[str, dict] = json.loads(stream.readline())
    validate_result_inventory(inventory)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Collect only into a new result; preserve earlier evidence")
    if destination.parent.resolve(strict=True) != destination.parent:
        raise ValueError("Result parent must be an original directory")
    if reference is not None:
        if reference.resolve(strict=True) != reference:
            raise ValueError("Result reference must be an original path")
        for _path in input_entries(reference):
            continue
    free_before = shutil.disk_usage(destination.parent).free
    report = {"cloned_files": 0, "transferred_files": 0, "transferred_bytes": 0}
    selected = []
    for name, entry in sorted(inventory.items()):
        target = destination if name == "." else destination / name
        if "sha256" not in entry:
            target.mkdir(mode=0o700)
            continue
        candidate = (reference if name == "." else reference / name) if reference is not None else None
        if candidate is not None and candidate.is_file() and candidate.stat().st_dev == destination.parent.stat().st_dev:
            with candidate.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() == entry["sha256"]:
                    try:
                        clone_input(candidate, target)
                    except OSError as error:
                        if error.errno not in (errno.EXDEV, errno.ENOTSUP, errno.ENOTTY, errno.ENOSYS) or target.exists():
                            raise
                    else:
                        target.chmod(0o600)
                        report["cloned_files"] += 1
                        continue
        selected.append(name)
    requests.write(json.dumps(selected).encode() + b"\n")
    requests.flush()
    for name in selected:
        with (destination if name == "." else destination / name).open("xb") as target:
            transfer_result_bytes(stream, target, inventory[name]["size"])
        report["transferred_files"] += 1
        report["transferred_bytes"] += inventory[name]["size"]
    actual = result_inventory(destination)
    if {name: {key: value for key, value in entry.items() if key != "mode"} for name, entry in actual.items()} != {
        name: {key: value for key, value in entry.items() if key != "mode"} for name, entry in inventory.items()
    }:
        raise ValueError("Collected result hashes differ; preserve all evidence")
    for name, entry in reversed(sorted(inventory.items())):
        (destination if name == "." else destination / name).chmod(entry["mode"])
    report["observed_bytes_added"] = free_before - shutil.disk_usage(destination.parent).free
    return report


def locked_file(path: Path) -> BinaryIO:
    return os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), "r+b")


def try_lock(handle: BinaryIO) -> bool:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


@dataclass
class Slot:
    index: int
    handle: BinaryIO
    queue_id: str


class RunQueue:
    def __init__(self, root: Path, slots: int) -> None:
        self.root = root
        self.slots = slots
        self.ticket: Path | None = None
        self.handle: BinaryIO | None = None
        self.queue_id = ""

    @contextmanager
    def control(self) -> Generator[None, None, None]:
        with locked_file(self.root / "queue.lock") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def __enter__(self) -> RunQueue:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.control():
            identity = self.root / "identity"
            if not identity.exists():
                pending = self.root / "identity.next"
                pending.write_text(uuid.uuid4().hex)
                pending.replace(identity)
            self.queue_id = identity.read_text()
            configured = self.root / "slots"
            if configured.exists() and int(configured.read_text()) != self.slots:
                for path in self.root.glob("slot-*.lock"):
                    with locked_file(path) as handle:
                        if not try_lock(handle):
                            raise ValueError("Change slot count only after running work is cleaned up")
                self.first_live_ticket()
                if list(self.root.glob("ticket-*")) or [path for path in self.root.glob("custody-*") if path.suffix != ".next"]:
                    raise ValueError("Change slot count only after all queued and owned work is cleaned up")
            if not configured.exists() or int(configured.read_text()) != self.slots:
                pending = self.root / "slots.next"
                pending.write_text(str(self.slots))
                pending.replace(configured)
            sequence = self.root / "sequence"
            number = int(sequence.read_text()) + 1 if sequence.exists() else 1
            pending = self.root / "sequence.next"
            pending.write_text(str(number))
            pending.replace(sequence)
            ticket = self.root / f"ticket-{number:020d}"
            handle = locked_file(ticket)
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.ticket = ticket
            self.handle = handle
        return self

    def first_live_ticket(self) -> Path | None:
        for ticket in sorted(self.root.glob("ticket-*")):
            if ticket == self.ticket:
                return ticket
            with locked_file(ticket) as handle:
                if not try_lock(handle):
                    return ticket
                ticket.unlink()
        return None

    @contextmanager
    def acquire(self) -> Generator[Slot, None, None]:
        while True:
            with self.control():
                if self.first_live_ticket() == self.ticket:
                    for index in range(self.slots):
                        handle = locked_file(self.root / f"slot-{index}.lock")
                        if try_lock(handle):
                            assert self.ticket is not None
                            self.ticket.unlink()
                            break
                        handle.close()
                    else:
                        handle = None
                else:
                    handle = None
            if handle is not None:
                break
            time.sleep(0.05)
        with handle:
            yield Slot(index, handle, self.queue_id)

    def __exit__(self, *_: object) -> None:
        if self.ticket is not None:
            self.ticket.unlink(missing_ok=True)
        if self.handle is not None:
            self.handle.close()


@dataclass(frozen=True)
class Settings:
    slots: int = 1
    cpus: float = 4
    memory: int = 8 * 1024**3
    executor: list[str] | None = None

    @classmethod
    def load(cls, home: Path) -> Settings:
        path = home / CONFIG_PATH
        data = json.loads(path.read_text()) if path.exists() else {}
        if set(data) - {"slots", "cpus", "memory", "executor"}:
            raise ValueError("Unknown host heavy-run setting")
        result = cls(**data)
        if result.executor is not None and (
            not isinstance(result.executor, list)
            or not result.executor
            or any(not isinstance(part, str) or not part or "\x00" in part for part in result.executor)
        ):
            raise ValueError("Executor must be a nonempty command argv array")
        if (
            type(result.slots) is not int
            or result.slots < 1
            or type(result.memory) is not int
            or result.memory < 1024**2
            or isinstance(result.cpus, bool)
            or not math.isfinite(result.cpus)
            or result.cpus <= 0
        ):
            raise ValueError("Slots, CPUs and memory bytes must be positive")
        return result

    def limits(self, count: int) -> tuple[str, str]:
        nanos = int(Decimal(str(self.cpus)) * 10**9) // count
        memory = self.memory // count
        if nanos < MIN_NANO_CPUS or memory < MIN_MEMORY:
            raise ValueError("Each container needs at least 0.01 CPUs and 6 MiB of memory")
        return f"{nanos // 10**9}.{nanos % 10**9:09d}", str(memory)


def validate_manifest(manifest: object) -> None:
    if not isinstance(manifest, dict) or set(manifest) - {"containers", "job", "outputs"}:
        raise ValueError("Expected a run manifest with containers, job and optional outputs")
    containers = manifest.get("containers")
    if not isinstance(containers, list) or not containers:
        raise ValueError("Provide containers")
    names = set()
    for container in containers:
        if not isinstance(container, dict) or set(container) - {"name", "image", "command", "environment", "copies"}:
            raise ValueError("Unsupported container fields; Docker flags and host mounts are unsupported")
        name = container.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", name) or name in names:
            raise ValueError("Provide unique container names")
        names.add(name)
        image = container.get("image")
        if not isinstance(image, str) or not image or image.startswith("-"):
            raise ValueError("Provide a cached Docker image")
        command = container.get("command", [])
        if not isinstance(command, list) or any(not isinstance(part, str) for part in command):
            raise ValueError("Container command must be an argv array")
        environment = container.get("environment", {})
        if not isinstance(environment, dict) or any(
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
            or not isinstance(value, str)
            or "\n" in value
            or "\r" in value
            for key, value in environment.items()
        ):
            raise ValueError("Environment must map variable names to single-line strings; copy files for multiline configuration")
        copies = container.get("copies", [])
        if not isinstance(copies, list):
            raise ValueError("Copies must be an array")
        for copy in copies:
            if (
                not isinstance(copy, dict)
                or not isinstance(copy.get("source"), str)
                or not isinstance(copy.get("destination"), str)
                or not copy["destination"].startswith("/")
                or ".." in Path(copy["destination"]).parts
                or not Path(copy["source"]).exists()
            ):
                raise ValueError("Copy inputs must exist and container destinations must be absolute strings")
            if type(copy.get("overlay", False)) is not bool:
                raise ValueError("Copy overlay must be a boolean")
            if "retained_from" in copy and not copy.get("overlay"):
                raise ValueError("Retained inputs require an overlay and the configured private executor")
            if copy.get("overlay"):
                destination = Path(copy["destination"])
                if (
                    destination == Path("/")
                    or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(destination))
                    or not Path(copy["source"]).is_dir()
                ):
                    raise ValueError("Overlay copies require a directory and a dedicated destination")
                if any(
                    other is not copy
                    and (destination.is_relative_to(other["destination"]) or Path(other["destination"]).is_relative_to(destination))
                    for other in copies
                ):
                    raise ValueError("Overlay destinations cannot overlap another input")
    if not isinstance(manifest.get("job"), str) or manifest["job"] not in names:
        raise ValueError("Select a job container")
    outputs = manifest.get("outputs", [])
    if not isinstance(outputs, list):
        raise ValueError("Outputs must be an array")
    output_names = set()
    for output in outputs:
        if (
            not isinstance(output, dict)
            or not isinstance(output.get("container"), str)
            or output["container"] not in names
            or not isinstance(output.get("name"), str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]+", output["name"])
            or output["name"] in output_names
            or not isinstance(output.get("source"), str)
            or not output["source"].startswith("/")
        ):
            raise ValueError("Provide a uniquely named container output with an absolute source path")
        output_names.add(output["name"])


def docker_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("DOCKER_")}


def overlay_lower_digest(root: Path, hashes: dict[str, str] | None = None) -> str:
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(root)) or root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError("Overlay lower must be an unaliased local directory without mount option delimiters")
    digest = hashlib.sha256()
    for path in input_entries(root):
        metadata = path.lstat()
        required_mode = 0o007 if path.is_dir() else 0o006
        if metadata.st_mode & required_mode != required_mode:
            raise ValueError("Overlay lower must already have container-copy permissions; prepare a compatible frozen input")
        entry = [
            str(path.relative_to(root)),
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_gid,
        ]
        if path.is_file():
            with path.open("rb") as stream:
                file_digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if hashes is not None:
                hashes[str(path.relative_to(root))] = file_digest
            entry.append(file_digest)
        digest.update(json.dumps(entry).encode())
    return LOWER_DIGEST_PREFIX + digest.hexdigest()


def local_endpoint(home: Path, log: TextIO) -> str:
    endpoint = subprocess.run(
        ["docker", "--config", str(home / ".docker"), "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
        stdout=subprocess.PIPE,
        stderr=log,
        text=True,
        env=docker_environment(),
        timeout=30,
        check=True,
    ).stdout.strip()
    if not endpoint.startswith("unix://"):
        raise ValueError("Heavy runs require a local Unix Docker socket")
    return endpoint


def container_input_mode(mode: int, *, directory: bool) -> int:
    return 0o777 if directory or mode & 0o111 else 0o666


def container_copy_mode(info: tarfile.TarInfo) -> tarfile.TarInfo:
    if info.isdir() or info.isfile():
        info.mode = container_input_mode(info.mode, directory=info.isdir())
    return info


def legacy_lower_proven(lower: Path) -> bool:
    """Accept a pre-content label only when the job's frozen inventory proves the exact files in this lower.

    Legacy labels embedded device and inode numbers that no record preserves, so they cannot be recomputed.
    complete.json records file bytes only, so every entry must also be a plain staged entry owned by this receiver.
    """
    inventory = lower.parent.parent / "complete.json"
    if lower.parent.name != "inputs":
        return False
    try:
        if not stat.S_ISREG(inventory.lstat().st_mode) or inventory.resolve(strict=True) != inventory:
            return False
        recorded = json.loads(inventory.read_text())["hashes"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not isinstance(recorded, dict):
        return False
    prefix = f"inputs/{lower.name}/"
    expected = {name.removeprefix(prefix): value for name, value in recorded.items() if name.startswith(prefix)}
    actual: dict[str, str] = {}
    overlay_lower_digest(lower, actual)
    if not actual or actual != expected:
        return False
    recorded_directories = {parent for name in actual for parent in Path(name).parents}
    for path in [lower, *lower.rglob("*")]:
        metadata = path.lstat()
        directory = stat.S_ISDIR(metadata.st_mode)
        if (
            (metadata.st_uid, metadata.st_gid) != (os.geteuid(), os.getegid())
            or stat.S_IMODE(metadata.st_mode) != (0o777 if directory else 0o666)
            or (directory and path.relative_to(lower) not in recorded_directories)
        ):
            return False
    return True


class Docker:
    def __init__(self, slot: Slot, log: TextIO, endpoint: str, evidence: Path | None = None, staging: str | None = None) -> None:
        if not endpoint.startswith("unix://"):
            raise ValueError("Heavy runs require a local Unix Docker socket")
        self.slot = slot
        self.log = log
        self.endpoint = endpoint
        self.label = f"{LABEL}={slot.queue_id}:{slot.index}"
        self.environment = docker_environment()
        self.prefix = ["docker", "--host", endpoint]
        self.lower_digests: dict[Path, str] = {}
        self.overlay_volumes: dict[tuple[Path, str], str] = {}
        self.evidence = evidence.resolve() if evidence is not None else None
        self.staging = staging
        self.staging_created = False
        self.storage: dict[str, int] = {}
        self.free_before = 0

    def staging_root(self) -> Path:
        if self.evidence is None or self.staging is None:
            raise ValueError("Stage inputs only inside the queued prepare callback")
        if not isinstance(self.staging, str) or not re.fullmatch(re.escape(STAGING_PREFIX) + r"[0-9a-f]{32}", self.staging):
            raise RuntimeError("Invalid staging ownership; preserve files")
        return self.evidence.parent / self.staging

    def stage_input(self, source: Path) -> Path:
        root = self.staging_root()
        if source.is_symlink():
            raise ValueError("Use a manifest reference to stream a symlinked original")
        source = source.resolve(strict=True)
        if source.is_relative_to(root) or source in root.parents:
            raise ValueError("The original must be separate from owned staging")
        paths = list(input_entries(source))
        if not self.staging_created:
            root.mkdir(mode=0o700)
            with (root / ".owner").open("x") as marker:
                marker.write(self.label)
                marker.flush()
                os.fsync(marker.fileno())
            self.staging_created = True
            self.free_before = shutil.disk_usage(root).free
            self.storage.update(cloned_files=0, copied_files=0, copied_bytes=0)
        destination = root / str(len(list(root.iterdir())) - 1)
        for path in paths:
            target = destination / path.relative_to(source)
            if path.is_dir():
                target.mkdir(mode=0o700)
                continue
            if path.stat().st_dev == root.stat().st_dev:
                clone_input(path, target)
                self.storage["cloned_files"] += 1
            else:
                with path.open("rb") as original, target.open("xb") as output:
                    while chunk := original.read(1024 * 1024):
                        self.storage["copied_bytes"] += output.write(chunk)
                self.storage["copied_files"] += 1
            target.chmod(stat.S_IMODE(path.stat().st_mode))
        self.storage["observed_bytes_added"] = self.free_before - shutil.disk_usage(root).free
        return destination

    @staticmethod
    def prepare_staging_removal(descriptor: int) -> None:
        clear_staged_flags(descriptor)
        os.fchmod(descriptor, 0o700)
        for _, directories, files, current in os.fwalk(dir_fd=descriptor):
            for name in directories + files:
                metadata = os.stat(name, dir_fd=current, follow_symlinks=False)
                directory = stat.S_ISDIR(metadata.st_mode)
                if not directory and (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1):
                    continue
                if not directory and sys.platform != "darwin":
                    continue
                child = os.open(name, getattr(os, "O_EVTONLY", os.O_RDONLY) | os.O_NOFOLLOW, dir_fd=current)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                        raise RuntimeError("Staging changed during normalization; preserve files")
                    clear_staged_flags(child)
                    if directory:
                        os.fchmod(child, 0o700)
                finally:
                    os.close(child)

    def collect_staged_inputs(self) -> None:
        if self.staging is None or self.evidence is None:
            return
        root = self.staging_root()
        parent = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                descriptor = os.open(root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                return
            try:
                marker = os.open(".owner", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                try:
                    metadata = os.fstat(marker)
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or os.read(marker, 1024) != self.label.encode():
                        raise RuntimeError("Foreign staging ownership; preserve files")
                finally:
                    os.close(marker)
                if not shutil.rmtree.avoids_symlink_attacks:
                    raise RuntimeError("Symlink-resistant collection is unavailable; preserve files")
                free_before = shutil.disk_usage(root.parent).free
                self.prepare_staging_removal(descriptor)
                current = os.stat(root.name, dir_fd=parent, follow_symlinks=False)
                opened = os.fstat(descriptor)
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    raise RuntimeError("Staging changed during collection; preserve files")
                shutil.rmtree(root.name, dir_fd=parent)
                self.storage["observed_bytes_reclaimed"] = shutil.disk_usage(root.parent).free - free_before
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)

    def finish_staging(self, *, recovery: bool = False) -> str | None:
        if not self.staging_created and not recovery:
            return None
        try:
            self.collect_staged_inputs()
        except (OSError, ValueError, RuntimeError, NotImplementedError) as error:
            message = f"Staging collection failed; preserved {self.staging!r} beside {self.evidence}: {error}"
            self.log.write(message + "\n")
            if self.evidence is not None and self.evidence.is_dir():
                try:
                    (self.evidence / "staging-error.json").write_text(
                        json.dumps({"error": message, "directory": self.staging, "owner": self.label}) + "\n"
                    )
                except OSError as report_error:
                    self.log.write(f"Staging report unavailable; original directory is named above: {report_error}\n")
            return message
        return None

    def call(self, *args: str, timeout: int | None = 60) -> str:
        result = subprocess.run(
            [*self.prefix, *args],
            stdout=subprocess.PIPE,
            stderr=self.log,
            text=True,
            env=self.environment,
            pass_fds=(self.slot.handle.fileno(),),
            timeout=timeout,
        )
        if result.returncode:
            raise RuntimeError(f"Docker {args[0]} failed; see private run.log")
        return result.stdout.strip()

    def containers(self) -> list[str]:
        return self.call("ps", "-aq", "--filter", f"label={self.label}").split()

    def cleanup(self, log: TextIO | None) -> None:
        for container in self.containers():
            if log is not None:
                self.collect_logs(container, log)
            self.call("rm", "--force", "--volumes", container)
        if self.containers():
            raise RuntimeError("Owned containers remain; slot cannot admit work")
        self.cleanup_overlay_volumes()
        networks = self.call("network", "ls", "-q", "--filter", f"label={self.label}").split()
        for network in networks:
            self.call("network", "rm", network)
        if self.call("network", "ls", "-q", "--filter", f"label={self.label}").split():
            raise RuntimeError("Owned networks remain; slot cannot admit work")

    def owned_volume(self, name: str) -> dict:
        volume = json.loads(self.call("volume", "inspect", name))[0]
        key, value = self.label.split("=", 1)
        if volume.get("Name") != name or volume.get("Driver") != "local" or (volume.get("Labels") or {}).get(key) != value:
            raise RuntimeError("Overlay volume ownership differs; preserve custody")
        return volume

    @staticmethod
    def stage_overlay_input(root: Path) -> None:
        if root.resolve(strict=True) != root or not root.is_dir():
            raise ValueError("Stage only a new unaliased overlay input directory")
        paths = list(input_entries(root))
        for path in paths:
            path.chmod(container_input_mode(path.stat().st_mode, directory=path.is_dir()))

    def cleanup_overlay_volumes(self) -> None:
        names = self.call("volume", "ls", "-q", "--filter", f"label={self.label}").split()
        volumes = [self.owned_volume(name) for name in names]
        lowers = {}
        for volume in volumes:
            options = volume.get("Options") or {}
            if not options:
                continue
            fields = options.get("o", "").split(",")
            if (
                options.keys() != {"type", "device", "o"}
                or options["type"] != "overlay"
                or options["device"] != "overlay"
                or len(fields) != 4
                or not fields[0].startswith("lowerdir=")
                or fields[-1] != "userxattr"
            ):
                raise RuntimeError("Owned overlay options differ; preserve custody")
            lower = Path(fields[0].removeprefix("lowerdir="))
            expected = (volume.get("Labels") or {}).get(LOWER_DIGEST_LABEL)
            if lower in lowers and lowers[lower] != expected:
                raise RuntimeError("Shared overlay lower bindings differ")
            lowers[lower] = expected
        for lower, expected in lowers.items():
            if LEGACY_LOWER_DIGEST.fullmatch(expected or ""):
                if not legacy_lower_proven(lower):
                    raise RuntimeError("Legacy overlay label lacks complete content evidence; preserve original input custody")
            elif overlay_lower_digest(lower) != expected:
                raise RuntimeError("Overlay lower changed during execution; preserve original input custody")
        for volume in sorted(volumes, key=lambda item: not bool(item.get("Options"))):
            self.call("volume", "rm", volume["Name"])
        if self.call("volume", "ls", "-q", "--filter", f"label={self.label}").split():
            raise RuntimeError("Owned overlay volumes remain; slot cannot admit work")

    def create_overlay(self, copy: dict, name: str) -> str:
        if os.uname().sysname != "Linux":
            raise ValueError("Overlay copies require a local Linux Docker engine; no copy fallback")
        lower = Path(copy["source"])
        if lower not in self.lower_digests:
            self.lower_digests[lower] = overlay_lower_digest(lower)
        lower_digest = self.lower_digests[lower]
        backing_name = name + "-backing"
        self.call("volume", "create", "--label", self.label, backing_name)
        backing = self.owned_volume(backing_name)
        private = Path(backing["Mountpoint"])
        docker_root = Path(self.call("info", "--format", "{{.DockerRootDir}}"))
        if (
            backing.get("Options")
            or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(private))
            or private != docker_root / "volumes" / backing_name / "_data"
            or private.resolve(strict=True) != private
            or any(private.iterdir())
        ):
            raise RuntimeError("Overlay writable backing is not an empty private volume")
        for directory in (private / "upper", private / "work"):
            directory.mkdir()
            directory.chmod(0o777)
        options = {
            "type": "overlay",
            "device": "overlay",
            "o": f"lowerdir={lower},upperdir={private}/upper,workdir={private}/work,userxattr",
        }
        arguments = [
            "volume",
            "create",
            "--driver",
            "local",
            "--label",
            self.label,
            "--label",
            f"{LOWER_DIGEST_LABEL}={lower_digest}",
        ]
        for key, value in options.items():
            arguments.extend(["--opt", f"{key}={value}"])
        self.call(*arguments, name)
        if self.owned_volume(name).get("Options") != options:
            raise RuntimeError("Overlay volume options differ")
        return name

    def observe_overlay_lower(self, root: Path) -> dict[str, str]:
        hashes: dict[str, str] = {}
        self.lower_digests[root] = overlay_lower_digest(root, hashes)
        return hashes

    def collect_logs(self, container: str, log: TextIO) -> None:
        try:
            subprocess.run(
                [*self.prefix, "logs", container],
                stdout=log,
                stderr=log,
                env=self.environment,
                pass_fds=(self.slot.handle.fileno(),),
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as error:
            log.write(f"Log collection failed: {type(error).__name__}\n")

    def copy_input(self, identifier: str, copy: dict) -> None:
        if copy.get("overlay"):
            return
        with subprocess.Popen(
            [*self.prefix, "cp", "-", f"{identifier}:/"],
            stdin=subprocess.PIPE,
            stdout=self.log,
            stderr=self.log,
            env=self.environment,
            pass_fds=(self.slot.handle.fileno(),),
        ) as process:
            assert process.stdin is not None
            with (
                process.stdin,
                tarfile.open(fileobj=process.stdin, mode="w|") as archive,
            ):
                archive.add(
                    Path(copy["source"]).resolve(),
                    arcname=copy["destination"].lstrip("/") or ".",
                    filter=container_copy_mode,
                )
            if process.wait():
                raise RuntimeError("Docker input copy failed; see private run.log")

    def create(self, container: dict, network: str, cpus: str, memory: str, evidence: Path, *, workload: bool = False) -> str:
        args = [
            "create",
            "--pull",
            "never",
            "--name",
            f"{network}-{container['name']}",
            "--label",
            self.label,
            "--cpus",
            cpus,
            "--memory",
            memory,
            "--memory-swap",
            memory,
            "--log-driver",
            "local",
            "--restart",
            "no",
            "--network",
            network,
            "--network-alias",
            container["name"],
        ]
        for index, copy in enumerate(container.get("copies", [])):
            if copy.get("overlay"):
                binding = (Path(copy["source"]), copy["destination"])
                if binding not in self.overlay_volumes:
                    self.overlay_volumes[binding] = self.create_overlay(copy, f"{network}-{container['name']}-{index}")
                name = self.overlay_volumes[binding]
                args.extend(
                    [
                        "--mount",
                        f"type=volume,src={name},dst={copy['destination']},volume-nocopy",
                    ]
                )
        env_file = evidence / f"{container['name']}.env"
        environment: dict[str, str] = container.get("environment") or {}
        try:
            if environment:
                env_file.write_text("".join(f"{name}={value}\n" for name, value in environment.items()))
                args.extend(["--env-file", str(env_file)])
            command = container.get("command", [])
            if workload:
                config = json.loads(self.call("image", "inspect", "--format", "{{json .Config}}", container["image"]))
                command = [*(config.get("Entrypoint") or []), *(command or config.get("Cmd") or [])]
                if not command:
                    raise ValueError("The workload image requires an entrypoint or command")
                args.extend(["--entrypoint", "python3"])
                command = ["-I", "-c", WORKLOAD_ENTRYPOINT, *command]
            return self.call(*args, container["image"], *command)
        finally:
            env_file.unlink(missing_ok=True)

    def run(self, manifest: dict, settings: Settings, evidence: Path) -> tuple[int, dict[str, str]]:
        containers = manifest["containers"]
        cpus, memory = settings.limits(len(containers))
        network = f"heavy-{self.slot.index}-{uuid.uuid4().hex}"
        self.call("network", "create", "--internal", "--label", self.label, network)
        identifiers = {}
        for container in containers:
            identifier = self.create(container, network, cpus, memory, evidence, workload=container["name"] == manifest["job"])
            identifiers[container["name"]] = identifier
            for copy in container.get("copies", []):
                self.copy_input(identifier, copy)
        for identifier in identifiers.values():
            self.call("start", identifier)
        job = identifiers[manifest["job"]]
        while self.call("inspect", "--format", "{{.State.Running}}", job) == "true":
            time.sleep(0.2)
        return int(self.call("inspect", "--format", "{{.State.ExitCode}}", job)), identifiers

    def copy_outputs(self, manifest: dict, identifiers: dict[str, str], evidence: Path) -> None:
        for output in manifest.get("outputs", []):
            self.call("cp", f"{identifiers[output['container']]}:{output['source']}", str(evidence / output["name"]), timeout=None)


class Canceled(Exception):
    def __init__(self, signum: int) -> None:
        self.signum = signum


def cancel(signum: int, _frame: object) -> None:
    raise Canceled(signum)


@dataclass(frozen=True)
class Custody:
    endpoint: str
    evidence: Path
    staging: str | None = None

    @classmethod
    def load(cls, path: Path) -> Custody:
        data = json.loads(path.read_text())
        return cls(data["endpoint"], Path(data["evidence"]), data.get("staging"))

    def save(self, path: Path) -> None:
        pending = path.with_suffix(".next")
        with pending.open("w") as stream:
            json.dump({"endpoint": self.endpoint, "evidence": str(self.evidence.resolve()), "staging": self.staging}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def recover(slot: Slot, custody: Path, log: TextIO, endpoint: str) -> None:
    if not custody.exists():
        Docker(slot, log, endpoint).cleanup(None)
        return
    previous = Custody.load(custody)
    recovered = previous.evidence / "recovered.log"
    if recovered.is_absolute() and recovered.parent.is_dir():
        with recovered.open("a") as previous_log:
            docker = Docker(slot, previous_log, previous.endpoint, previous.evidence, previous.staging)
            docker.cleanup(previous_log)
            docker.finish_staging(recovery=True)
    else:
        docker = Docker(slot, log, previous.endpoint, previous.evidence, previous.staging)
        docker.cleanup(None)
        docker.finish_staging(recovery=True)
    if previous.evidence.is_absolute() and previous.evidence.is_dir():
        for env_file in previous.evidence.glob("*.env"):
            env_file.unlink()
    custody.unlink()


def run_in_slot(
    manifest: dict,
    evidence: Path,
    settings: Settings,
    home: Path,
    slot: Slot,
    log: TextIO,
    prepare: Callable[[Docker], dict] | None,
) -> dict:
    receipt: dict = {"exit_code": None, "cleanup_complete": True, "slot": slot.index}
    custody = home / STATE_PATH / f"custody-{slot.index}"
    try:
        endpoint = local_endpoint(home, log)
        recover(slot, custody, log, endpoint)
        staging = STAGING_PREFIX + uuid.uuid4().hex
        docker = Docker(slot, log, endpoint, evidence, staging)
        Custody(endpoint, evidence, staging).save(custody)
        receipt["cleanup_complete"] = False
        try:
            if prepare is not None:
                manifest = prepare(docker)
                validate_manifest(manifest)
                settings.limits(len(manifest["containers"]))
            receipt["exit_code"], identifiers = docker.run(manifest, settings, evidence)
            docker.copy_outputs(manifest, identifiers, evidence)
        finally:
            for signum in CANCEL_SIGNALS:
                signal.signal(signum, signal.SIG_IGN)
            try:
                docker.cleanup(log)
                receipt["cleanup_complete"] = True
                try:
                    if error := docker.finish_staging():
                        receipt["staging_cleanup_error"] = error
                        receipt["exit_code"] = receipt["exit_code"] or 1
                finally:
                    custody.unlink()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                receipt["cleanup_error"] = str(error)
                receipt["exit_code"] = receipt["exit_code"] or 1
            finally:
                if docker.storage:
                    receipt["storage"] = docker.storage
    except Canceled as error:
        receipt["exit_code"] = 128 + error.signum
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        receipt["error"] = str(error)
        receipt["exit_code"] = receipt["exit_code"] or 1
    return receipt


def run(
    manifest: dict,
    evidence: Path,
    settings: Settings,
    home: Path,
    prepare: Callable[[Docker], dict] | None = None,
    complete: Callable[[dict], None] | None = None,
) -> int:
    evidence.mkdir(mode=0o700, parents=True)
    receipt: dict = {"exit_code": None, "cleanup_complete": True}
    with (evidence / "run.log").open("w") as log:
        try:
            with RunQueue(home / STATE_PATH, settings.slots) as queue, queue.acquire() as slot:
                receipt = run_in_slot(manifest, evidence, settings, home, slot, log, prepare)
                (evidence / "result.json").write_text(json.dumps(receipt, indent=2) + "\n")
                if complete is not None:
                    try:
                        complete(receipt)
                    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                        (evidence / "delivery-error.json").write_text(json.dumps({"error": str(error)}) + "\n")
                        return 1
        except Canceled as error:
            receipt["exit_code"] = 128 + error.signum
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            receipt["error"] = str(error)
            receipt["exit_code"] = receipt["exit_code"] or 1
        finally:
            for signum in CANCEL_SIGNALS:
                signal.signal(signum, signal.SIG_IGN)
            (evidence / "result.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return int(receipt["exit_code"])


def main(*, home: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="Queue an isolated local heavy Docker run; keep all output private.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    for signum in CANCEL_SIGNALS:
        signal.signal(signum, cancel)
    home = home if home is not None else Path(pwd.getpwuid(os.getuid()).pw_dir)
    try:
        settings = Settings.load(home)
        manifest = json.loads(args.manifest.read_text())
        validate_manifest(manifest)
        if settings.executor is not None:
            os.execvp(
                settings.executor[0],
                [
                    *settings.executor,
                    "--manifest",
                    str(args.manifest.resolve()),
                    "--evidence",
                    str(args.evidence.resolve()),
                ],
            )
        if any("retained_from" in copy for container in manifest["containers"] for copy in container.get("copies", [])):
            raise ValueError("Retained inputs require the configured private executor; no local fallback")
        settings.limits(len(manifest["containers"]))
        result = run(manifest, args.evidence, settings, home)
    except Canceled as error:
        return 128 + error.signum
    except (
        KeyError,
        TypeError,
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": type(error).__name__,
                    "evidence": str(args.evidence),
                }
            )
        )
        return 1
    print(json.dumps({"ok": result == 0, "exit_code": result, "evidence": str(args.evidence)}))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
