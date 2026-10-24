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
from contextlib import contextmanager
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
    write_row(log_path(session, create=True), hashlib.sha256(f"{repo}#{number}".encode()).hexdigest() + ".json", row)


def attempts_path(session: str, *, create: bool = False) -> Path:
    path = log_path(session, create=create) / "attempts"
    if create:
        path.mkdir(exist_ok=True, mode=0o700)
    lease.require_private(path.lstat(), stat.S_ISDIR)
    return path


@contextmanager
def attempt(session: str, repo: str, number: int) -> Iterator[None]:
    record(session, repo, number)
    row = {"session": session, "repository": repo, "record": number, "pid": os.getpid(), "ended": False}
    directory, filename = attempts_path(session, create=True), f"{uuid.uuid4().hex}.json"
    write_row(directory, filename, row)
    try:
        yield
    finally:
        write_row(directory, filename, {**row, "ended": True})


def process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Attempt(NamedTuple):
    target: tuple[str, int]
    pid: int
    ended: bool

    @property
    def running(self) -> bool:
        return not self.ended and process_running(self.pid)


def read_rows(directory: Path) -> Iterator[dict]:
    for path in sorted(directory.glob("*.json")):
        lease.require_private(path.lstat(), stat.S_ISREG)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, encoding="utf-8") as stream:
            lease.require_private(os.fstat(stream.fileno()), stat.S_ISREG)
            row = json.load(stream)
        if not isinstance(row, dict):
            raise ValueError("claim journal has an invalid record")
        yield row


def read(session: str) -> set[tuple[str, int]]:
    try:
        directory = log_path(session)
    except FileNotFoundError:
        return set()
    return {target(row, session) for row in read_rows(directory)}


def attempts(session: str) -> list[Attempt]:
    try:
        directory = attempts_path(session)
    except FileNotFoundError:
        return []
    found = []
    for row in read_rows(directory):
        pid, ended = row.get("pid"), row.get("ended")
        if type(pid) is not int or pid <= 0 or type(ended) is not bool:
            raise ValueError("claim journal has an invalid attempt")
        found.append(Attempt(target(row, session), pid, ended))
    return found
