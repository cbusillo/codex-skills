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
import re
import signal
import subprocess
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, TextIO

LABEL = "org.codex-skills.heavy-slot"


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
        path = home / ".config/codex-skills/heavy-run.json"
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


def validate_manifest(manifest: dict) -> None:
    containers = manifest["containers"]
    names = [container["name"] for container in containers]
    if not names or len(set(names)) != len(names) or manifest["job"] not in names:
        raise ValueError("Provide unique containers and a job container")
    if set(manifest) - {"containers", "job", "outputs"}:
        raise ValueError("Unknown manifest field")
    for container in containers:
        if set(container) - {"name", "image", "command", "environment", "copies"}:
            raise ValueError("Unknown container field; Docker flags and host mounts are unsupported")
        if not re.fullmatch(r"[a-z][a-z0-9-]*", container["name"]):
            raise ValueError("Invalid container name")
        if not isinstance(container["image"], str) or not container["image"] or container["image"].startswith("-"):
            raise ValueError("Provide a Docker image")
        command = container.get("command", [])
        if not isinstance(command, list) or any(not isinstance(part, str) for part in command):
            raise ValueError("Container command must be an argv array")
        for copy in container.get("copies", []):
            if not Path(copy["source"]).exists() or not copy["destination"].startswith("/"):
                raise ValueError("Copy inputs must exist and container destinations must be absolute")
    for output in manifest.get("outputs", []):
        if output["container"] not in names or not re.fullmatch(r"[a-zA-Z0-9_-]+", output["name"]):
            raise ValueError("Invalid evidence output")
        if not output["source"].startswith("/"):
            raise ValueError("Container output sources must be absolute")


class Docker:
    def __init__(self, slot: Slot, log: TextIO) -> None:
        self.slot = slot
        self.log = log
        self.label = f"{LABEL}={slot.index}"
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}
        }
        endpoint = subprocess.run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.strip()
        if not endpoint.startswith("unix://"):
            raise ValueError("Heavy runs require a local Unix Docker socket")
        self.prefix = ["docker", "--host", endpoint]

    def call(self, *args: str, timeout: int = 60) -> str:
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

    def cleanup(self) -> None:
        for container in self.containers():
            subprocess.run(
                [*self.prefix, "logs", container],
                stdout=self.log,
                stderr=self.log,
                env=self.environment,
                pass_fds=(self.slot.handle.fileno(),),
                timeout=60,
            )
            self.call("rm", "--force", "--volumes", container)
        if self.containers():
            raise RuntimeError("Owned containers remain; slot cannot admit work")
        networks = self.call("network", "ls", "-q", "--filter", f"label={self.label}").split()
        for network in networks:
            self.call("network", "rm", network)
        if self.call("network", "ls", "-q", "--filter", f"label={self.label}").split():
            raise RuntimeError("Owned networks remain; slot cannot admit work")

    def run(self, manifest: dict, settings: Settings, evidence: Path) -> int:
        self.cleanup()
        network = f"heavy-{self.slot.index}-{uuid.uuid4().hex}"
        self.call("network", "create", "--internal", "--label", self.label, network)
        containers = manifest["containers"]
        cpus = str(settings.cpus / len(containers))
        memory = str(settings.memory // len(containers))
        identifiers = {}
        for container in containers:
            args = [
                "create",
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
                "--workdir",
                "/workspace",
            ]
            for name, value in container.get("environment", {}).items():
                args.extend(["--env", f"{name}={value}"])
            identifier = self.call(*args, container["image"], *container.get("command", []))
            identifiers[container["name"]] = identifier
            for copy in container.get("copies", []):
                source = Path(copy["source"]).resolve()
                source_path = f"{source}/." if source.is_dir() else str(source)
                self.call("cp", source_path, f"{identifier}:{copy['destination']}", timeout=600)
        for identifier in identifiers.values():
            self.call("start", identifier)
        job = identifiers[manifest["job"]]
        while self.call("inspect", "--format", "{{.State.Running}}", job) == "true":
            time.sleep(0.2)
        exit_code = int(self.call("inspect", "--format", "{{.State.ExitCode}}", job))
        for output in manifest.get("outputs", []):
            self.call("cp", f"{identifiers[output['container']]}:{output['source']}", str(evidence / output["name"]), timeout=600)
        return exit_code


class Canceled(Exception):
    def __init__(self, signum: int) -> None:
        self.signum = signum


def cancel(signum: int, _frame: object) -> None:
    raise Canceled(signum)


def run(manifest: dict, evidence: Path, settings: Settings, root: Path) -> int:
    evidence.mkdir(mode=0o700, parents=True)
    receipt: dict = {"exit_code": None, "cleanup_complete": False}
    with (evidence / "run.log").open("w") as log, RunQueue(root, settings.slots) as queue:
        with queue.acquire() as slot:
            receipt["slot"] = slot.index
            custody = root / f"custody-{slot.index}"
            custody.write_text(str(evidence.resolve()))
            docker = Docker(slot, log)
            try:
                receipt["exit_code"] = docker.run(manifest, settings, evidence)
            except Canceled as error:
                receipt["exit_code"] = 128 + error.signum
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                receipt["error"] = str(error)
                receipt["exit_code"] = 1
            finally:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                try:
                    docker.cleanup()
                    receipt["cleanup_complete"] = True
                    custody.unlink()
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    receipt["cleanup_error"] = str(error)
                    receipt["exit_code"] = 1
                (evidence / "result.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return int(receipt["exit_code"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Queue an isolated local heavy Docker run; keep all output private.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    try:
        settings = Settings.load(Path.home())
        manifest = json.loads(args.manifest.read_text())
        validate_manifest(manifest)
        result = run(manifest, args.evidence, settings, Path.home() / ".local/state/codex-skills/heavy-run")
    except Canceled as error:
        return 128 + error.signum
    except (KeyError, TypeError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"ok": False, "error": type(error).__name__, "evidence": str(args.evidence)}))
        return 1
    print(json.dumps({"ok": result == 0, "exit_code": result, "evidence": str(args.evidence)}))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
