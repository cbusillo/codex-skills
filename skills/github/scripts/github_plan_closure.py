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
_OBSERVATION = ContextVar("closure_observation", default=None)
RECEIPTS = ContextVar("observed_closures", default={})
LOG_CACHE = ContextVar("observed_closure_log", default=None)


class LeaseBusy(ValueError):
    pass


def require_canonical_identity(session: str) -> None:
    """Native aliases cannot select another freeze or lease; legacy opaque IDs remain readable."""
    from github_plan_sessions import process_liveness
    canonical = process_liveness().native_uuid(session)
    if canonical is not None and canonical != session:
        raise ValueError("use the canonical native thread UUID")


def lease_path(session: str) -> Path:
    require_canonical_identity(session)
    return closed_log().parent / "session-ownership" / (hashlib.sha256(session.encode()).hexdigest() + ".lock")


@contextmanager
def ownership(session: str, *, create: bool = False):
    """Nonblocking per-thread lease, shared by closure, resume and admission."""
    path = lease_path(session)
    held = _LEASES.get()
    if held is not None and session in held:
        yield True
        return
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
        _LEASES.set({*(held or set()), session})
        try:
            yield True
        finally:
            # Independent RPCs can complete out of order. Resetting an enter
            # token would erase another held lease or resurrect a released one.
            _LEASES.set((_LEASES.get() or set()) - {session})


def lease_state(session: str) -> dict:
    """Read action and freeze state from their existing ownership file."""
    try:
        fd = os.open(lease_path(session), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("unsafe native-session action evidence")
        text = stream.read(4096)
        if not text:
            return {}
        value = json.loads(text)
        if (not isinstance(value, dict) or value.get("session_id") != session
                or not (value.get("action_pending") is True or value.get("successor_admitted") is True)):
            raise ValueError("native-session action evidence is unavailable")
        if value.get("action_pending") is True:
            if not isinstance(value.get("action_at"), str):
                raise ValueError("native action timestamp is unavailable")
            timestamp = datetime.fromisoformat(value["action_at"])
            if timestamp.tzinfo is None:
                raise ValueError("native action timestamp requires a timezone")
        return value


def action_time(session: str) -> datetime | None:
    """Read the durable per-source barrier, including sources with no receipt."""
    value = lease_state(session)
    if value.get("action_pending") is True:
        timestamp = datetime.fromisoformat(value["action_at"])
        return timestamp
    return None


def action_pending(session: str) -> bool:
    return action_time(session) is not None


def action_homes(value: dict) -> dict:
    scope = value.get("action_homes")
    if scope is None:
        return {"known": [], "unscoped": value.get("action_pending") is True}
    if (not isinstance(scope, dict) or not isinstance(scope.get("known"), list)
            or type(scope.get("unscoped")) is not bool
            or any(not isinstance(home, str) or not Path(home).is_absolute() for home in scope["known"])):
        raise ValueError("native action home coverage is unavailable")
    return {"known": sorted({str(Path(home).resolve()) for home in scope["known"]}), "unscoped": scope["unscoped"]}


def require_fresh_action_verdict(entry: dict) -> None:
    after = action_time(entry["session_id"])
    if after is None:
        return
    from github_plan_release import stamp
    from github_plan_sessions import native_helpers
    records = native_helpers()[1].read_jsonl(Path(entry["transcript"]))
    tail = [r for r in records if r.get("timestamp") and stamp(r["timestamp"]) > after]
    status = native_helpers()[1].summarize(tail, entry["harness"])
    # Post-exit recovery needs a fresh declaration, not the live claim gate.
    # The tail deliberately excludes older identity and delegated histories.
    if not status.get("declared_safe_verdict", status["safe_verdict"]):
        raise ValueError("native action needs a fresh final safe verdict before verified closure")


ROUTED_METHODS = {"thread/queue/add", "thread/queue/start", "turn/start", "turn/steer", "thread/resume"}


def write_lease_state(session: str, value: dict) -> None:
    if session not in (_LEASES.get() or set()):
        raise ValueError("native action evidence has no ownership lease")
    text = json.dumps({"session_id": session, **value}) if value else ""
    if len(text.encode()) > 4096:
        raise ValueError("native action home evidence exceeds its bounded ownership file")
    fd = os.open(lease_path(session), os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, "r+") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("unsafe native-session action evidence")
        stream.write(text)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())


def set_action_pending(session: str, pending: bool, *, method: str | None = None, home: str | Path | None = None) -> None:
    if session not in (_LEASES.get() or set()):
        raise ValueError("native action evidence has no ownership lease")
    value = lease_state(session)
    if pending:
        scope = action_homes(value)
        if home is None:
            scope["unscoped"] = True
        else:
            if not Path(home).is_absolute():
                raise ValueError("native action home must be absolute")
            scope["known"] = sorted({*scope["known"], str(Path(home).resolve())})
        value["action_homes"] = scope
        kind = "routed" if method in ROUTED_METHODS else "opaque"
        if value.get("action_pending") and value.get("action_kind") != "routed" and kind == "routed":
            write_lease_state(session, value)
            return  # Retain opaque chronology while still recording another home.
        value.update(action_pending=True, action_kind=kind,
                     action_at=datetime.now(timezone.utc).isoformat())
    else:
        for key in ("action_pending", "action_kind", "action_at", "action_homes"):
            value.pop(key, None)
        if value.keys() <= {"session_id"}:
            value = {}
    write_lease_state(session, value)


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
def observation(sessions: set[str], *, retain: set[str] | None = None):
    """Global reads never create lease files. Busy/missing proofs stay unknown."""
    with ExitStack() as local:
        group = _ADMISSION.get()
        stack, held = group if group is not None else (local, set())
        acquired = set()
        attempts = {}
        context = _OBSERVATION.set((stack, held, acquired, attempts))
        token = LOG_CACHE.set(None)
        try:
            candidates = source_records(sessions)
        finally:
            LOG_CACHE.reset(token)
        for session in sorted(sessions):
            try:
                if session in (_LEASES.get() or set()):
                    # Borrow only the currently live guard. Its owner controls
                    # its lifetime; admission must not retain fictitious holds.
                    acquired.add(session)
                    continue
                if session in held:
                    acquired.add(session)
                    continue
                # A known-live source cannot contribute end proof. Do not
                # retain its lease across native reads or claim round trips:
                # its own actions and approvals still need that lease.
                if action_pending(session) or candidates.get(session, {}).get("resumed_at"):
                    continue
                if candidates.get(session, {}).get("harness") == "codex":
                    from github_plan_native import verify_source_process
                    verify_source_process(candidates[session])
                with ExitStack() as attempt:
                    if not attempt.enter_context(ownership(session, create=group is not None)):
                        continue
                    # An action may have completed between the unleased read
                    # above and acquisition. Release immediately in that case.
                    if action_pending(session):
                        continue
                    owned = attempt.pop_all()
                    stack.callback(owned.close)
                    attempts[session] = owned
                    held.add(session)
                    acquired.add(session)
            except (OSError, ValueError, KeyError, TypeError, ImportError, subprocess.SubprocessError):
                continue
        try:
            yield acquired
        finally:
            _OBSERVATION.reset(context)
            if retain is not None:
                for session, owned in attempts.items():
                    if session not in retain:
                        owned.close()
                        held.discard(session)


@contextmanager
def pending_observation(session: str):
    """Acquire only after native routing metadata says this source may be quiet."""
    if session in (_LEASES.get() or set()):
        yield True
        return
    with ExitStack() as attempt:
        if not attempt.enter_context(ownership(session)):
            yield False
            return
        yield True
        group = _OBSERVATION.get()
        if group is not None:
            stack, held, acquired, attempts = group
            owned = attempt.pop_all()
            stack.callback(owned.close)
            attempts[session] = owned
            held.add(session)
            acquired.add(session)


def inherit_observed_leases():
    """Transfer still-open async proof guards back into the synchronous owner."""
    group = _OBSERVATION.get()
    if group is not None:
        _LEASES.set({*(_LEASES.get() or set()), *group[2]})


def held_sources() -> set[str]:
    """Current owner's retained native source leases, independent of proof format."""
    return set(_LEASES.get() or set())


def log_rows() -> list[dict]:
    path = closed_log()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe closed-session log")
    rows = json.loads(path.read_text())
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ValueError("malformed closed-session log")
    return rows


def argv_signature(argv: list[str]) -> str:
    # ps cannot preserve argv boundaries or literal prompt whitespace. Only
    # the immutable option prefix binds a client; the ingress guards attaches.
    prefix = argv[1:argv.index("--")] if "--" in argv else argv[1:]
    return hashlib.sha256(" ".join(" ".join(prefix).split()).encode()).hexdigest()


def remote_endpoints(argv: list[str]) -> list[str]:
    prefix = argv[:argv.index("--")] if "--" in argv else argv
    return ([prefix[i + 1] for i, arg in enumerate(prefix[:-1]) if arg == "--remote"]
            + [arg.split("=", 1)[1] for arg in prefix if arg.startswith("--remote=")])


def prune_registrations(rows: list[dict]) -> list[dict]:
    def alive(row):
        if type(row.get("launch_pid")) is not int or row["launch_pid"] <= 0:
            return True
        try:
            os.kill(row["launch_pid"], 0)
            return True
        except ProcessLookupError:
            return False
        except (OSError, KeyError, TypeError):
            return True  # Unknown identity is never deleted as presumed dead.
    return [row for row in rows if row.get("record_kind") not in {"managed_launch", "managed_proxy"} or alive(row)]


def register_proxy(home: str, endpoint: str) -> dict:
    metadata = launch_metadata(os.getpid(), os.urandom(16).hex(), home)
    row = {"record_kind": "managed_proxy", **metadata, "endpoint": endpoint,
           "argv_digest": argv_signature(sys.argv)}
    with store_lock(closed_log(), create=True):
        rows = prune_registrations(log_rows() if closed_log().exists() else [])
        rows = [r for r in rows if r.get("record_kind") != "managed_proxy" or r.get("launch_pid") != os.getpid()]
        replace_rows([*rows, row])
    return row


def remove_registration(pid: int, generation: str) -> None:
    if not closed_log().exists():
        return
    with store_lock(closed_log()):
        try:
            rows = log_rows()
        except FileNotFoundError:
            return
        replace_rows(prune_registrations([r for r in rows if not (
            r.get("record_kind") in {"managed_launch", "managed_proxy"}
            and r.get("launch_pid") == pid and r.get("launch_generation") == generation)]))


def register_launch(argv: list[str], home: str, *, proxy: dict | None = None) -> None:
    """Register the managed fresh client's identity before exec replaces us."""
    if proxy and remote_endpoints(argv) != ["unix://" + proxy["endpoint"]]:
        raise ValueError("managed native client must use its guarded ingress")
    metadata = launch_metadata(os.getpid(), os.urandom(16).hex(), home)
    path = closed_log()
    with store_lock(path, create=True):
        rows = prune_registrations(log_rows() if path.exists() else [])
        # Reap only older records for this PID. PID and birth must both match
        # at observation; other clients' live records are never reassigned.
        rows = [r for r in rows if r.get("record_kind") != "managed_launch" or r.get("launch_pid") != os.getpid()]
        rows.append({"record_kind": "managed_launch", **metadata,
                     "argv_digest": argv_signature(argv), "proxy": proxy})
        replace_rows(rows)


def guarded_launch_endpoint(pid: int, argv: list[str]) -> str | None:
    """Return the bound home endpoint after proving a live guarded client."""
    try:
        identity = log_identity(closed_log().lstat())
        cache = LOG_CACHE.get()
        rows = cache[1] if cache and cache[0] == identity else log_rows()
        LOG_CACHE.set((identity, rows))
        matches = [r for r in rows if r.get("record_kind") in {"managed_launch", "managed_proxy"} and r.get("launch_pid") == pid]
        if not matches and any(r.get("record_kind") == "managed_launch" and isinstance(r.get("proxy"), dict) for r in rows):
            # The npm launcher waits for a native child instead of exec'ing.
            # Both processes must have the same guarded option prefix, and the
            # recorded parent must still have its original process birth.
            parent = int(subprocess.run(["ps", "-p", str(pid), "-o", "ppid="], check=True,
                                        capture_output=True, text=True, timeout=2).stdout.strip())
            matches = [r for r in rows if r.get("record_kind") == "managed_launch" and r.get("launch_pid") == parent]
            if len(matches) != 1 or not (
                process_started_at(parent).isoformat() == matches[0]["launch_started_at"]
                and process_started_at(parent) <= process_started_at(pid) <= datetime.now(timezone.utc)
            ):
                return None
        if len(matches) != 1:
            return None
        row = matches[0]
        if argv_signature(argv) != row["argv_digest"]:
            return None
        if row["launch_pid"] == pid and process_started_at(pid).isoformat() != row["launch_started_at"]:
            return None
        proxy = row if row["record_kind"] == "managed_proxy" else row.get("proxy")
        if not isinstance(proxy, dict) or proxy not in rows:
            return None
        if row["record_kind"] == "managed_launch" and remote_endpoints(argv) != ["unix://" + proxy["endpoint"]]:
            return None
        info = Path(proxy["endpoint"]).lstat()
        if (stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077
                and proxy["codex_home"] == row["codex_home"]
                and process_started_at(proxy["launch_pid"]).isoformat() == proxy["launch_started_at"]):
            return str(daemon_endpoint(Path(row["codex_home"])))
        return None
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None


def managed_launch(pid: int, argv: list[str], sockets: set[str]) -> bool:
    """Require guarded identity and independently observed daemon coverage."""
    endpoint = guarded_launch_endpoint(pid, argv)
    return endpoint is not None and endpoint in sockets


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
        deadline = time.monotonic() + 1
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LeaseBusy("shared closure log is busy; retry without native execution") from None
                time.sleep(0.01)
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


def resume_record(session: str, rows: list[dict]) -> dict | None:
    """Check known source refusals without leases, invalidation or publication."""
    require_canonical_identity(session)
    matches = [r for r in rows if r.get("session_id") == session]
    if len(matches) > 1:
        raise ValueError("duplicate closed generations; preserve the source")
    row = matches[0] if matches else None
    if lease_state(session).get("successor_admitted") or row is not None and row.get("successor_admitted"):
        raise ValueError("this generation handed off its work; start a new thread and claim through gh-plan.py")
    return row


@contextmanager
def resume_guard(session: str, *, method: str | None = None, home: str | Path | None = None):
    """Invalidate the closed generation before an exact native resume/queue."""
    with ownership(session, create=True):
        if lease_state(session).get("successor_admitted"):
            raise ValueError("this generation handed off its work; start a new thread and claim through gh-plan.py")
        path = closed_log()
        # The source lease also serializes closure publication for this ID.
        # A new thread has no receipt to invalidate and need not wait behind
        # unrelated store mutations before its first turn.
        if path.exists() and any(r.get("session_id") == session for r in log_rows()):
            with store_lock(path, create=True):
                rows = log_rows()
                row = resume_record(session, rows)
                if row is not None:
                    row["closure_evidence"] = None
                    row["resumed_at"] = datetime.now(timezone.utc).isoformat()
                    replace_rows(rows)
        # Native acknowledgement may precede deferred execution or transcript
        # persistence. Authored releases must not bypass this invalidation.
        set_action_pending(session, True, method=method, home=home)
        yield


def admit_sources(sessions: set[str]) -> None:
    """Freeze selected sources before a claim writes, with their leases held."""
    for session in sessions:
        require_canonical_identity(session)
        if session not in (_LEASES.get() or set()):
            raise ValueError("source admission has no ownership lease")
    values = {session: lease_state(session) for session in sessions}
    for session, value in values.items():
        write_lease_state(session, {**value, "successor_admitted": True})


def closed_log() -> Path:
    # Neither an account switch nor a shell's CODE_HOME may select a different
    # freeze/lease namespace. This is user-wide coordination, not runtime data.
    return Path.home() / ".code/skill-data/supervisor-closed-sessions.json"


def daemon_endpoint(home: Path) -> Path:
    from github_plan_sessions import native_helpers
    native_helpers()
    from iterm_tab import codex_daemon_endpoint
    return codex_daemon_endpoint(home)


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


def transcript_facts(session: str, primary: str, *, include_status: bool = False, additional_homes: set[Path] | None = None, additional_roots: set[Path] | frozenset[Path] | None = None, reader=None) -> dict:
    """Authenticate stable native transcripts without deciding final-safe authority."""
    from github_plan_sessions import codex_homes, native_helpers
    from github_plan_release import stamp
    deadline = time.monotonic() + 3
    paths = {Path(primary).resolve(strict=True)}
    def fail(error):
        raise error
    homes = {*codex_homes(strict=True, configured_only=True), *(additional_homes or set())}
    roots = set()
    for home in homes:
        for root in (home / "sessions", home / "archived_sessions"):
            try:
                root.stat()
            except FileNotFoundError:
                continue
            roots.add(root)
    for root in additional_roots or set():
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("source transcript root is unavailable")
        roots.add(root)
    for root in sorted(roots):
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
        records, digest, lines = [], hashlib.sha256(), []
        with path.open("rb") as stream:
            for line in stream:
                if time.monotonic() > deadline:
                    raise TimeoutError("native closure read exceeded its budget")
                digest.update(line)
                if reader is not None:
                    lines.append(line.decode("utf-8"))
                if line.strip() and reader is None:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise TypeError("native transcript record must be an object")
                    native_helpers()[1].validate_record(record)
                    records.append(record)
        if reader is not None:
            records, truncated = reader(lines)
            if time.monotonic() > deadline:
                raise TimeoutError("native closure read exceeded its budget")
        identities = {r.get("payload", {}).get("id") for r in records if r.get("type") == "session_meta"}
        if identities != {session}:
            raise ValueError("native transcript identity mismatch")
        status = native_helpers()[1].summarize(records, "codex")
        after = path.stat()
        if transcript_identity(before) != transcript_identity(after):
            raise ValueError("native transcript is changing")
        timestamps = [r["timestamp"] for r in records if r.get("timestamp")]
        if not timestamps:
            raise ValueError("native transcript lacks timestamped evidence")
        latest = max(timestamps, key=stamp)
        snapshots[str(path)] = {"identity": transcript_identity(after),
                                "sha256": digest.hexdigest(), "latest": latest}
        if reader is not None:
            snapshots[str(path)]["truncated"] = truncated
        if include_status:
            snapshots[str(path)]["status"] = {key: status[key] for key in (
                "safe_verdict", "timestamp", "turn_id", "turn_started_at", "turn_completed_at", "message_at", "actual_turn_ids", "background_tasks",
                "active_realtime_sessions", "realtime_coverage")}
            snapshots[str(path)]["status"]["declared_safe_verdict"] = status.get("declared_safe_verdict", status["safe_verdict"])
    return snapshots


def transcripts(session: str, primary: str, *, include_status: bool = False, additional_homes: set[Path] | None = None) -> dict:
    snapshots = transcript_facts(session, primary, include_status=True, additional_homes=additional_homes)
    # This wrapper supplies final-declaration evidence for independently
    # verified post-exit closure; neutral readers keep the live claim verdict.
    if any(not snapshot["status"].get("declared_safe_verdict", snapshot["status"]["safe_verdict"])
           for snapshot in snapshots.values()):
        raise ValueError("native transcript lacks a final safe verdict")
    if not include_status:
        snapshots = {path: {key: value for key, value in snapshot.items() if key != "status"}
                     for path, snapshot in snapshots.items()}
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
        if action_pending(session):
            return None
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
        from github_plan_sessions import native_helpers
        entry = native_helpers()[1].normalize_ledger_entry(
            path, {k: v for k, v in matching[0].items() if not k.startswith("_")})
        proof = entry.get("closure_evidence")
        if (entry.get("harness") != "codex" or not isinstance(proof, dict) or entry.get("resumed_at")
                or (home is not None and proof.get("codex_home") != str(home.resolve()))):
            return None

        import release_session_claims
        from github_plan_release import stamp
        ended = stamp(entry["ended_at"])
        if entry.get("process_exit_verified") is not True or ended > datetime.now(timezone.utc):
            return None
        release_session_claims.verify_closed(entry, "claim-observer", ended_at=ended)
        if evidence(entry) != entry["closure_evidence"]:
            return None
        return entry if receipt_current(entry) else None
    except (OSError, ValueError, KeyError, TypeError, ImportError, subprocess.SubprocessError):
        return None


def process_started_at(pid: int) -> datetime:
    from github_plan_sessions import native_helpers
    native_helpers()
    import session_liveness
    return session_liveness.process_started_at(pid)


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
                   resumed_generation={**metadata, "pid": metadata["launch_pid"], "tty": tty, "transcript": transcript})
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
        from github_plan_sessions import native_helpers
        normalize = native_helpers()[1].normalize_ledger_entry
        cache = LOG_CACHE.get()
        rows = cache[1] if cache and cache[0] == log_identity(closed_log().lstat()) else log_rows()
        rows = [r for r in rows if r.get("session_id") == entry["session_id"]]
        return len(rows) == 1 and normalize(closed_log(), rows[0]) == normalize(closed_log(), entry)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def source_records(sessions: set[str]) -> dict:
    if not sessions:
        return {}
    try:
        before = log_identity(closed_log().lstat())
        rows = log_rows()
        if before != log_identity(closed_log().lstat()):
            return {}
        LOG_CACHE.set((before, rows))
        from github_plan_sessions import native_helpers
        normalize = native_helpers()[1].normalize_ledger_entry
        return {session: normalize(closed_log(), matches[0]) for session in sessions
                if len(matches := [r for r in rows if r.get("session_id") == session]) == 1}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def candidate_records(sessions: set[str]) -> dict:
    return {session: row for session, row in source_records(sessions).items()
            if isinstance(row.get("closure_evidence"), dict)}


def log_identity(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode, info.st_uid


def native_process_check(entry: dict) -> None:
    from github_plan_sessions import native_helpers
    entry = native_helpers()[1].normalize_ledger_entry(closed_log(), entry)
    import release_session_claims
    from github_plan_release import stamp
    release_session_claims.verify_closed(entry, "claim-observer", ended_at=stamp(entry["ended_at"]))
