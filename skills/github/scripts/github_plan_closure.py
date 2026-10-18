# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Host-local closure evidence. A receipt alone never proves an idle thread ended."""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from datetime import datetime, timezone

_ADMISSION = ContextVar("closure_admission", default=None)
_LEASES = ContextVar("closure_leases", default=None)
RECEIPTS = ContextVar("observed_closures", default={})
LOG_CACHE = ContextVar("observed_closure_log", default=None)


class LeaseBusy(ValueError):
    pass


def lease_path(session: str) -> Path:
    return closed_log().parent / "session-ownership" / (hashlib.sha256(session.encode()).hexdigest() + ".lock")


@contextmanager
def ownership(session: str, *, create: bool = False):
    """Nonblocking per-thread lease, shared by closure, resume and admission."""
    held = _LEASES.get()
    if held is not None and session in held:
        yield True
        return
    path = lease_path(session)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, (os.O_RDWR if create else os.O_RDONLY) | os.O_NOFOLLOW | (os.O_CREAT if create else 0), 0o600)
    except FileNotFoundError:
        yield False
        return
    with os.fdopen(fd, "r+" if create else "r") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("unsafe native-session ownership lease")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LeaseBusy("native-session closure, resume or admission is in progress") from error
        token = _LEASES.set({*(held or set()), session})
        try:
            yield True
        finally:
            _LEASES.reset(token)


@contextmanager
def admission():
    """Retain source leases until the claim's final readback or failure."""
    with ExitStack() as stack:
        token = _ADMISSION.set((stack, set()))
        try:
            yield
        finally:
            _ADMISSION.reset(token)


@contextmanager
def observation(sessions: set[str]):
    """Global reads never create lease files. Busy/missing proofs stay unknown."""
    with ExitStack() as local:
        group = _ADMISSION.get()
        stack, held = group if group is not None else (local, set())
        acquired = set()
        for session in sorted(sessions):
            try:
                if session in held or stack.enter_context(ownership(session)):
                    held.add(session)
                    acquired.add(session)
            except (OSError, ValueError):
                continue
        yield acquired


def log_rows() -> list[dict]:
    path = closed_log()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe closed-session log")
    rows = json.loads(path.read_text())
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ValueError("malformed closed-session log")
    return rows


def register_launch(argv: list[str], home: str) -> None:
    """Register the managed fresh client's identity before exec replaces us."""
    metadata = launch_metadata(os.getpid(), os.urandom(16).hex(), home)
    path = closed_log()
    with store_lock(path, create=True):
        rows = log_rows() if path.exists() else []
        # Reap only older records for this PID. PID and birth must both match
        # at observation; other clients' live records are never reassigned.
        rows = [r for r in rows if r.get("record_kind") != "managed_launch" or r.get("launch_pid") != os.getpid()]
        rows.append({"record_kind": "managed_launch", **metadata,
                     "argv_digest": hashlib.sha256(" ".join(argv[1:]).encode()).hexdigest()})
        replace_rows(rows)


def managed_launch(pid: int, argv: list[str], sockets: set[str]) -> bool:
    """Only a cooperating fresh remote client with the same process birth."""
    try:
        rows = log_rows()
        matches = [r for r in rows if r.get("record_kind") == "managed_launch" and r.get("launch_pid") == pid]
        if len(matches) != 1:
            return False
        row = matches[0]
        if hashlib.sha256(" ".join(argv[1:]).encode()).hexdigest() != row["argv_digest"]:
            return False
        endpoint = str(Path(row["codex_home"]) / "app-server-control/app-server-control.sock")
        if endpoint not in sockets:
            return False
        return process_started_at(pid).isoformat() == row["launch_started_at"]
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return False


@contextmanager
def store_lock(path: Path, *, create: bool = False):
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0), 0o600)
    except FileNotFoundError:
        yield
        return
    with os.fdopen(fd, "r+") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("unsafe closed-session mutation lock")
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def replace_rows(rows: list[dict]) -> None:
    path = closed_log()
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(rows, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def resume_guard(session: str):
    """Invalidate the closed generation before an exact native resume/queue."""
    with ownership(session, create=True):
        path = closed_log()
        if path.exists():
            with store_lock(path, create=True):
                rows = log_rows()
                matches = [r for r in rows if r.get("session_id") == session]
                if len(matches) > 1:
                    raise ValueError("duplicate closed generations; preserve the source")
                if matches:
                    row = matches[0]
                    if row.get("successor_admitted"):
                        raise ValueError("this generation handed off its work; start a new thread and claim through gh-plan.py")
                    row["closure_evidence"] = None
                    row["resumed_at"] = datetime.now(timezone.utc).isoformat()
                    replace_rows(rows)
        yield


def admit_sources(sessions: set[str]) -> None:
    """Freeze selected sources before a claim writes, with their leases held."""
    if not sessions or not closed_log().exists():
        return
    with store_lock(closed_log(), create=True):
        rows = log_rows()
        for row in rows:
            if row.get("session_id") in sessions:
                if row["session_id"] not in (_LEASES.get() or set()):
                    raise ValueError("source admission has no ownership lease")
                row["successor_admitted"] = True
        replace_rows(rows)


def closed_log() -> Path:
    # CODEX_HOME varies by account. Successors must use the shared runtime home.
    root = Path(os.environ.get("CODE_HOME") or str(Path.home() / ".code")).expanduser()
    if not root.is_absolute():
        raise ValueError("the shared runtime home must be absolute")
    return root / "skill-data/supervisor-closed-sessions.json"


def host_identity() -> dict:
    if sys.platform == "darwin":
        boot = subprocess.run(["sysctl", "-n", "kern.bootsessionuuid"],
                              check=True, capture_output=True, text=True, timeout=5).stdout.strip()
    else:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if not boot:
        raise ValueError("host boot identity is unavailable")
    return {"boot": boot, "uid": os.getuid()}


def transcript_identity(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def transcripts(session: str, primary: str) -> dict:
    from github_plan_sessions import codex_homes, native_helpers
    from github_plan_release import stamp
    deadline = time.monotonic() + 3
    paths = {Path(primary).resolve(strict=True)}
    def fail(error):
        raise error
    for home in codex_homes(strict=True, configured_only=True):
        for root in (home / "sessions", home / "archived_sessions"):
            try:
                root.stat()
            except FileNotFoundError:
                continue
            for directory, _, names in os.walk(root, onerror=fail):
                if time.monotonic() > deadline:
                    raise TimeoutError("native closure scan exceeded its budget")
                for name in names:
                    if session not in name or not name.endswith(".jsonl"):
                        continue
                    path = Path(directory) / name
                    if time.monotonic() > deadline:
                        raise TimeoutError("native closure scan exceeded its budget")
                    paths.add(path.resolve(strict=True))
    snapshots = {}
    for path in sorted(paths):
        before = path.stat()
        records, digest = [], hashlib.sha256()
        with path.open("rb") as stream:
            for line in stream:
                if time.monotonic() > deadline:
                    raise TimeoutError("native closure read exceeded its budget")
                digest.update(line)
                if line.strip():
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise TypeError("native transcript record must be an object")
                    native_helpers()[1].validate_record(record)
                    records.append(record)
        identities = {r.get("payload", {}).get("id") for r in records if r.get("type") == "session_meta"}
        if identities != {session}:
            raise ValueError("native transcript identity mismatch")
        status = native_helpers()[1].summarize(records, "codex")
        after = path.stat()
        if transcript_identity(before) != transcript_identity(after) or not status["safe_verdict"]:
            raise ValueError("native transcript is changing or lacks a final safe verdict")
        timestamps = [r["timestamp"] for r in records if r.get("timestamp")]
        if not timestamps:
            raise ValueError("native transcript lacks timestamped evidence")
        latest = max(timestamps, key=stamp)
        snapshots[str(path)] = {"identity": transcript_identity(after),
                                "sha256": digest.hexdigest(), "latest": latest}
    return snapshots


def evidence(entry: dict) -> dict:
    from github_plan_sessions import codex_homes
    if not Path(entry["codex_home"]).expanduser().is_absolute() or not Path(entry["transcript"]).is_absolute():
        raise ValueError("closure identity paths must be absolute")
    home = Path(entry["codex_home"]).expanduser().resolve(strict=True)
    primary = Path(entry["transcript"]).resolve(strict=True)
    homes = codex_homes(strict=True, configured_only=True)
    if home not in homes or not any(
        primary.is_relative_to((home / root).resolve()) for root in ("sessions", "archived_sessions")
    ):
        raise ValueError("closure account home and transcript identity disagree")
    snapshots = transcripts(entry["session_id"], str(primary))
    from github_plan_release import stamp
    if any(stamp(snapshot["latest"]) > stamp(entry["ended_at"]) for snapshot in snapshots.values()):
        raise ValueError("native transcript activity is newer than the closure")
    return {"host": host_identity(), "codex_home": str(home),
            "homes": sorted(str(h) for h in homes), "transcripts": snapshots,
            "launch": {k: entry[k] for k in ("launch_pid", "launch_generation", "launch_started_at") if k in entry}}


def verified_record(session: str, home: Path | None = None) -> dict | None:
    """Optional, read-only proof; unavailable evidence never changes native liveness."""
    try:
        path = closed_log()
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o077):
            return None
        cache = LOG_CACHE.get()
        rows = cache[1] if cache and cache[0] == log_identity(before) else json.loads(path.read_text())
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            return None
        matching = [r for r in rows if r.get("session_id") == session]
        if len(matching) != 1:
            return None
        entry = {k: v for k, v in matching[0].items() if not k.startswith("_")}
        proof = entry.get("closure_evidence")
        if (entry.get("harness") != "codex" or not isinstance(proof, dict) or entry.get("resumed_at")
                or (home is not None and proof.get("codex_home") != str(home.resolve()))):
            return None
        if isinstance(entry.get("tty"), str):
            entry["tty"] = entry["tty"].removeprefix("/dev/")
        from github_plan_sessions import codex_homes, native_helpers
        native_helpers()
        import release_session_claims
        from github_plan_release import stamp
        ended = stamp(entry["ended_at"])
        if entry.get("process_exit_verified") is not True or ended > datetime.now(timezone.utc):
            return None
        release_session_claims.verify_closed(entry, "claim-observer", ended_at=ended)
        if evidence(entry) != entry["closure_evidence"]:
            return None
        after = path.lstat()
        if (before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_ino, after.st_mtime_ns, after.st_size):
            return None
        return entry
    except (OSError, ValueError, KeyError, TypeError, ImportError, subprocess.SubprocessError):
        return None


def process_started_at(pid: int) -> datetime:
    value = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], check=True,
                           capture_output=True, text=True, timeout=2,
                           env={**os.environ, "TZ": "UTC", "LC_ALL": "C"}).stdout.strip()
    return datetime.strptime(value, "%a %b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc)


def launch_metadata(pid: int, generation: str, home: str) -> dict:
    """Capture identity while the launcher still owns a live child."""
    return {"codex_home": str(Path(home).expanduser().resolve()), "launch_pid": pid,
            "launch_generation": generation, "launch_started_at": process_started_at(pid).isoformat()}


def resumed_launch(session: str, metadata: dict, transcript: str, tty: str) -> None:
    """Record a new generation while the resume helper holds the source lease."""
    if session not in (_LEASES.get() or set()):
        raise ValueError("resume launch has no ownership lease")
    path = closed_log()
    with store_lock(path, create=True):
        rows = log_rows() if path.exists() else []
        matches = [r for r in rows if r.get("session_id") == session]
        if len(matches) > 1:
            raise ValueError("duplicate native session generations")
        if not matches:
            rows.append({"session_id": session, "harness": "codex", "transcript": transcript})
        row = next(r for r in rows if r.get("session_id") == session)
        row.update(closure_evidence=None, resumed_at=datetime.now(timezone.utc).isoformat(),
                   resumed_generation={**metadata, "pid": metadata["launch_pid"], "tty": tty})
        replace_rows(rows)


def idle_eligible(entry: dict) -> bool:
    """Legacy receipts do not acquire launch provenance retroactively."""
    try:
        from github_plan_release import stamp
        return (type(entry.get("launch_pid")) is int and entry["launch_pid"] > 0
                and isinstance(entry.get("launch_generation"), str) and bool(entry["launch_generation"])
                and stamp(entry["launch_started_at"]) <= stamp(entry["ended_at"]))
    except (KeyError, TypeError, ValueError):
        return False


def receipt_current(entry: dict) -> bool:
    try:
        cache = LOG_CACHE.get()
        rows = cache[1] if cache and cache[0] == log_identity(closed_log().lstat()) else log_rows()
        rows = [r for r in rows if r.get("session_id") == entry["session_id"]]
        return len(rows) == 1 and rows[0] == entry
    except (OSError, ValueError, KeyError, TypeError):
        return False


def candidate_records(sessions: set[str]) -> dict:
    if not sessions:
        return {}
    try:
        before = log_identity(closed_log().lstat())
        rows = log_rows()
        if before != log_identity(closed_log().lstat()):
            return {}
        LOG_CACHE.set((before, rows))
        return {session: matches[0] for session in sessions
                if len(matches := [r for r in rows if r.get("session_id") == session]) == 1
                and isinstance(matches[0].get("closure_evidence"), dict)}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def log_identity(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode, info.st_uid


def native_process_check(entry: dict) -> None:
    from github_plan_sessions import native_helpers
    native_helpers()
    import release_session_claims
    from github_plan_release import stamp
    release_session_claims.verify_closed(entry, "claim-observer", ended_at=stamp(entry["ended_at"]))
