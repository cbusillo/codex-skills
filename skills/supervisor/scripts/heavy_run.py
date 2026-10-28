#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import pwd
import re
import signal
import subprocess
import tarfile
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import BinaryIO, TextIO

LABEL = "org.codex-skills.heavy-slot"
CONFIG_PATH = Path(".config/codex-skills/heavy-run.json")
STATE_PATH = Path(".local/state/codex-skills/heavy-run")
MIN_NANO_CPUS = 10**7
MIN_MEMORY = 6 * 1024**2
CANCEL_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


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


class RunQueue:
    def __init__(self, root: Path, slots: int) -> None:
        self.root = root
        self.slots = slots
        self.ticket: Path | None = None
        self.handle: BinaryIO | None = None

    @contextmanager
    def control(self) -> Generator[None, None, None]:
        with locked_file(self.root / "queue.lock") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def __enter__(self) -> RunQueue:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.control():
            configured = self.root / "slots"
            if configured.exists() and int(configured.read_text()) != self.slots:
                for path in self.root.glob("slot-*.lock"):
                    with locked_file(path) as handle:
                        if not try_lock(handle):
                            raise ValueError("Change slot count only after running work is cleaned up")
                self.first_live_ticket()
                if list(self.root.glob("ticket-*")) or list(self.root.glob("custody-*")):
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
            yield Slot(index, handle)

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

    @classmethod
    def load(cls, home: Path) -> Settings:
        path = home / CONFIG_PATH
        data = json.loads(path.read_text()) if path.exists() else {}
        if set(data) - {"slots", "cpus", "memory"}:
            raise ValueError("Unknown host heavy-run setting")
        result = cls(**data)
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


def container_copy_mode(info: tarfile.TarInfo) -> tarfile.TarInfo:
    if info.isdir():
        info.mode = 0o777
    elif info.isfile():
        info.mode = 0o777 if info.mode & 0o111 else 0o666
    return info


class Docker:
    def __init__(self, slot: Slot, log: TextIO, endpoint: str) -> None:
        if not endpoint.startswith("unix://"):
            raise ValueError("Heavy runs require a local Unix Docker socket")
        self.slot = slot
        self.log = log
        self.endpoint = endpoint
        self.label = f"{LABEL}={slot.index}"
        self.environment = docker_environment()
        self.prefix = ["docker", "--host", endpoint]

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
        networks = self.call("network", "ls", "-q", "--filter", f"label={self.label}").split()
        for network in networks:
            self.call("network", "rm", network)
        if self.call("network", "ls", "-q", "--filter", f"label={self.label}").split():
            raise RuntimeError("Owned networks remain; slot cannot admit work")

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
        with subprocess.Popen(
            [*self.prefix, "cp", "-", f"{identifier}:/"],
            stdin=subprocess.PIPE,
            stdout=self.log,
            stderr=self.log,
            env=self.environment,
            pass_fds=(self.slot.handle.fileno(),),
        ) as process:
            assert process.stdin is not None
            with process.stdin, tarfile.open(fileobj=process.stdin, mode="w|") as archive:
                archive.add(
                    Path(copy["source"]).resolve(), arcname=copy["destination"].lstrip("/") or ".", filter=container_copy_mode
                )
            if process.wait():
                raise RuntimeError("Docker input copy failed; see private run.log")

    def create(self, container: dict, network: str, cpus: str, memory: str, evidence: Path) -> str:
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
        env_file = evidence / f"{container['name']}.env"
        environment: dict[str, str] = container.get("environment") or {}
        try:
            if environment:
                env_file.write_text("".join(f"{name}={value}\n" for name, value in environment.items()))
                args.extend(["--env-file", str(env_file)])
            return self.call(*args, container["image"], *container.get("command", []))
        finally:
            env_file.unlink(missing_ok=True)

    def run(self, manifest: dict, settings: Settings, evidence: Path) -> tuple[int, dict[str, str]]:
        containers = manifest["containers"]
        cpus, memory = settings.limits(len(containers))
        network = f"heavy-{self.slot.index}-{uuid.uuid4().hex}"
        self.call("network", "create", "--internal", "--label", self.label, network)
        identifiers = {}
        for container in containers:
            identifier = self.create(container, network, cpus, memory, evidence)
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

    @classmethod
    def load(cls, path: Path) -> Custody:
        data = json.loads(path.read_text())
        return cls(data["endpoint"], Path(data["evidence"]))

    def save(self, path: Path) -> None:
        pending = path.with_suffix(".next")
        pending.write_text(json.dumps({"endpoint": self.endpoint, "evidence": str(self.evidence.resolve())}))
        pending.replace(path)


def recover(slot: Slot, custody: Path, log: TextIO, endpoint: str) -> None:
    if not custody.exists():
        Docker(slot, log, endpoint).cleanup(None)
        return
    previous = Custody.load(custody)
    recovered = previous.evidence / "recovered.log"
    if recovered.is_absolute() and recovered.parent.is_dir():
        with recovered.open("a") as previous_log:
            Docker(slot, previous_log, previous.endpoint).cleanup(previous_log)
    else:
        Docker(slot, log, previous.endpoint).cleanup(None)
    custody.unlink()


def run(manifest: dict, evidence: Path, settings: Settings, home: Path) -> int:
    evidence.mkdir(mode=0o700, parents=True)
    root = home / STATE_PATH
    receipt: dict = {"exit_code": None, "cleanup_complete": True}
    with (evidence / "run.log").open("w") as log:
        try:
            with RunQueue(root, settings.slots) as queue, queue.acquire() as slot:
                receipt["slot"] = slot.index
                custody = root / f"custody-{slot.index}"
                endpoint = local_endpoint(home, log)
                recover(slot, custody, log, endpoint)
                docker = Docker(slot, log, endpoint)
                Custody(endpoint, evidence).save(custody)
                receipt["cleanup_complete"] = False
                try:
                    receipt["exit_code"], identifiers = docker.run(manifest, settings, evidence)
                    docker.copy_outputs(manifest, identifiers, evidence)
                finally:
                    for signum in CANCEL_SIGNALS:
                        signal.signal(signum, signal.SIG_IGN)
                    try:
                        docker.cleanup(log)
                        receipt["cleanup_complete"] = True
                        custody.unlink()
                    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                        receipt["cleanup_error"] = str(error)
                        receipt["exit_code"] = receipt["exit_code"] or 1
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
        settings.limits(len(manifest["containers"]))
        result = run(manifest, args.evidence, settings, home)
    except Canceled as error:
        return 128 + error.signum
    except (KeyError, TypeError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"ok": False, "error": type(error).__name__, "evidence": str(args.evidence)}))
        return 1
    print(json.dumps({"ok": result == 0, "exit_code": result, "evidence": str(args.evidence)}))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
