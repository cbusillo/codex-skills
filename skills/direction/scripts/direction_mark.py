#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Record that a daily direction turn just finished on this machine.

The session-start hook in `hooks/direction_check_hook.py` reads this marker
and reminds the owner while a turn is overdue. Only the direction agent runs
this, at the end of a turn the owner took part in. Weekly audits are not
marked here: `direction_audit.py` records its own completion per repository,
so an audit stamp means a real read-only audit ran.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

MARKER_NAME = "direction-last-check.json"


def marker_path(env: Mapping[str, str] | None = None) -> Path:
    """One marker for every host: DIRECTION_MARKER when set, else ~/.code/direction-last-check.json.

    Host home variables are deliberately not consulted. Codex sets CODEX_HOME for
    its hooks and Claude Code does not, so a lookup by those would give each host
    its own file and a check done in one would never clear the other's reminder.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    explicit = source.get("DIRECTION_MARKER")
    if explicit:
        return Path(explicit).expanduser()
    return Path(source.get("HOME", "~")).expanduser() / ".code" / MARKER_NAME


def utc_stamp(now: dt.datetime) -> str:
    return now.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load(path: Path) -> dict[str, object]:
    try:
        current = json.loads(path.read_text())
    except (OSError, ValueError):
        current = None
    current = current if isinstance(current, dict) else {}
    audits = current.get("audits")
    current["audits"] = audits if isinstance(audits, dict) else {}
    return current


@contextmanager
def marker_lock(path: Path) -> Iterator[Path]:
    """Serialize read/modify/replace on a stable sidecar, never the replaced inode."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "rb") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def save(path: Path, current: dict[str, object]) -> None:
    """Atomically replace the marker; the caller must hold marker_lock."""
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(current, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def mark_turn(path: Path, now: dt.datetime) -> dict[str, object]:
    with marker_lock(path) as path:
        current = load(path)
        current["turn"] = utc_stamp(now)
        save(path, current)
    return current


def mark_audit(path: Path, repo: str, now: dt.datetime) -> dict[str, object]:
    """Used by the audit script; a turn is implied because an audit is a turn."""
    with marker_lock(path) as path:
        current = load(path)
        stamp = utc_stamp(now)
        current["turn"] = stamp
        audits = current["audits"]
        assert isinstance(audits, dict)
        audits[repo] = stamp
        save(path, current)
    return current


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["turn"], help="only a daily turn can be marked by hand")
    parser.parse_args(argv)
    path = marker_path()
    written = mark_turn(path, dt.datetime.now(dt.timezone.utc))
    print(json.dumps({"ok": True, "marker": str(path), "turn": written["turn"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
