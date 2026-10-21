# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

import github_plan_lease as lease

REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def log_path(session: str, *, create: bool = False) -> Path:
    directory = Path.home() / ".code/skill-data/claim-log"
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lease.require_private(directory.lstat(), stat.S_ISDIR)
    return directory / f"{hashlib.sha256(session.encode()).hexdigest()}.jsonl"


def target(record: dict, session: str) -> tuple[str, int]:
    repo, number = record.get("repository"), record.get("record")
    if (
        record.get("session") != session
        or not isinstance(repo, str)
        or not REPOSITORY.fullmatch(repo)
        or type(number) is not int
        or number <= 0
    ):
        raise ValueError("claim journal has an invalid session or target")
    return repo, number


def record(session: str, repo: str, number: int) -> None:
    row = {"session": session, "repository": repo, "record": number}
    target(row, session)
    path = log_path(session, create=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        lease.require_private(os.fstat(fd), stat.S_ISREG)
        content = (json.dumps(row, sort_keys=True) + "\n").encode()
        if os.write(fd, content) != len(content):
            raise OSError("claim journal write was incomplete; do not post a claim")
        os.fsync(fd)
    finally:
        os.close(fd)


def read(session: str) -> set[tuple[str, int]]:
    try:
        path = log_path(session)
        lease.require_private(path.lstat(), stat.S_ISREG)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return set()
    with os.fdopen(fd, encoding="utf-8") as stream:
        lease.require_private(os.fstat(stream.fileno()), stat.S_ISREG)
        records = [json.loads(line) for line in stream]
    if any(not isinstance(row, dict) for row in records):
        raise ValueError("claim journal has an invalid record")
    return {target(row, session) for row in records}
