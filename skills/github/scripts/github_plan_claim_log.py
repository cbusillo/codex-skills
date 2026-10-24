# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import github_plan_lease as lease
from github_identity import REPOSITORY_PATTERN


def release_command(helper: Path, repo: str, number: int, claim_comment: int, session: str) -> str:
    return shlex.join(
        [
            "uv",
            "run",
            str(helper),
            "--repo",
            repo,
            "release",
            str(number),
            "--claim-comment",
            str(claim_comment),
            "--session",
            session,
            "--handoff-file",
            "<handoff.md>",
        ]
    )


def log_path(session: str, *, create: bool = False) -> Path:
    directory = Path.home() / ".code/skill-data/claim-log"
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lease.require_private(directory.lstat(), stat.S_ISDIR)
    path = directory / hashlib.sha256(session.encode()).hexdigest()
    if create:
        path.mkdir(exist_ok=True, mode=0o700)
    lease.require_private(path.lstat(), stat.S_ISDIR)
    return path


def target(record: dict, session: str) -> tuple[str, int]:
    repo, number = record.get("repository"), record.get("record")
    if (
        record.get("session") != session
        or not isinstance(repo, str)
        or not REPOSITORY_PATTERN.fullmatch(repo)
        or type(number) is not int
        or number <= 0
    ):
        raise ValueError("claim journal has an invalid session or target")
    return repo, number


def write_row(directory: Path, filename: str, row: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(row, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / filename)
        for parent in (directory, directory.parent):
            directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def record(session: str, repo: str, number: int) -> None:
    row = {"session": session, "repository": repo, "record": number}
    target(row, session)
    directory = log_path(session, create=True)
    write_row(attempts_path(session, create=True), f"{uuid.uuid4().hex}.json", {**row, "pid": os.getpid(), "group": os.getpgid(0)})
    write_row(directory, hashlib.sha256(f"{repo}#{number}".encode()).hexdigest() + ".json", row)


def attempts_path(session: str, *, create: bool = False) -> Path:
    path = log_path(session, create=create) / "attempts"
    if create:
        path.mkdir(exist_ok=True, mode=0o700)
    lease.require_private(path.lstat(), stat.S_ISDIR)
    return path


def process_running(pid: int, *, group: bool = False) -> bool:
    probe = os.killpg if group else os.kill
    try:
        probe(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Attempt(NamedTuple):
    identifier: str
    target: tuple[str, int]
    pid: int
    group: int

    @property
    def running(self) -> bool:
        return process_running(self.pid) or process_running(self.group, group=True)


def read_rows(directory: Path) -> Iterator[tuple[str, dict]]:
    for path in sorted(directory.glob("*.json")):
        lease.require_private(path.lstat(), stat.S_ISREG)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, encoding="utf-8") as stream:
            lease.require_private(os.fstat(stream.fileno()), stat.S_ISREG)
            row = json.load(stream)
        if not isinstance(row, dict):
            raise ValueError("claim journal has an invalid record")
        yield path.stem, row


def read(session: str) -> set[tuple[str, int]]:
    try:
        directory = log_path(session)
    except FileNotFoundError:
        return set()
    return {target(row, session) for _, row in read_rows(directory)}


def attempts(session: str) -> list[Attempt]:
    try:
        directory = attempts_path(session)
    except FileNotFoundError:
        return []
    found = []
    for identifier, row in read_rows(directory):
        pid, group = row.get("pid"), row.get("group")
        if any(type(value) is not int or value <= 0 for value in (pid, group)):
            raise ValueError("claim journal has an invalid attempt")
        found.append(Attempt(identifier, target(row, session), pid, group))
    return found
