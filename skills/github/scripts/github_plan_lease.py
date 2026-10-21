# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import hashlib
import os
import stat
import time
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


def renew(session: str) -> None:
    if not isinstance(session, str) or not session.strip():
        raise ValueError("lease renewal needs a native session ID")
    directory = lease_directory()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(lease_path(session), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("claim lease is not a regular file")
        os.utime(fd)
    finally:
        os.close(fd)


def renewed_at(session: str) -> float | None:
    try:
        info = lease_path(session).lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("claim lease is not a regular file owned by this user")
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
