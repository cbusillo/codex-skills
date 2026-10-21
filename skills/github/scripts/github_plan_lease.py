# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import hashlib
import os
import stat
import time
from collections.abc import Callable
from pathlib import Path

LEASE_MINUTES_VARIABLE = "GH_PLAN_CLAIM_LEASE_MINUTES"
DEFAULT_LEASE_MINUTES = 30.0
LIVE = "live"
EXPIRED = "expired"
UNKNOWN = "unknown"


def lease_minutes() -> float:
    configured = os.environ.get(LEASE_MINUTES_VARIABLE, "").strip()
    if not configured:
        return DEFAULT_LEASE_MINUTES
    minutes = float(configured)
    if not 0 < minutes < float("inf"):
        raise ValueError(f"{LEASE_MINUTES_VARIABLE} must be a positive number of minutes")
    return minutes


def lease_directory() -> Path:
    return Path.home() / ".code/skill-data/claim-leases"


def lease_path(session: str) -> Path:
    return lease_directory() / hashlib.sha256(session.encode()).hexdigest()


def private_store(*, create: bool) -> Path:
    directory = lease_directory()
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    require_private(directory.lstat(), stat.S_ISDIR)
    return directory


def require_private(info: os.stat_result, kind: Callable[[int], bool]) -> None:
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("claim lease store must be private to this user")


def renew(session: str) -> None:
    if not isinstance(session, str) or not session.strip():
        raise ValueError("lease renewal needs a native session ID")
    private_store(create=True)
    fd = os.open(lease_path(session), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        require_private(os.fstat(fd), stat.S_ISREG)
        os.utime(fd)
    finally:
        os.close(fd)


def renewed_at(session: str) -> float | None:
    try:
        private_store(create=False)
        info = lease_path(session).lstat()
    except FileNotFoundError:
        return None
    require_private(info, stat.S_ISREG)
    return info.st_mtime


def state(session: str, *, now: float | None = None) -> str:
    renewed = renewed_at(session)
    if renewed is None:
        return UNKNOWN
    elapsed = (time.time() if now is None else now) - renewed
    return EXPIRED if elapsed > lease_minutes() * 60 else LIVE


def renew_from_hook(payload: object) -> None:
    if isinstance(payload, dict) and isinstance(payload.get("session_id"), str):
        renew(payload["session_id"])
