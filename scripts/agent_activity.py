# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Count-only lifecycle projection shared by hooks and the passive observer."""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path

PRODUCER = "codex-skills"
STATES = {"working", "attention", "quiet", "completed", "failed", "cancelled"}
TERMINAL = {"completed", "failed", "cancelled"}
SOURCES = {"codex-status", "codex-turn", "claude-hook", "claude-inventory"}
OWN_FILE = re.compile(r"^codex-skills-[0-9a-f-]{36}[.]json$")
LEASE_SECONDS = 90
STATE = "agent-activity"  # Private producer state under CODE_HOME.
INBOX = "inbox"  # Producer outbox; Context Panel imports it. The App Group is never written here.
PRUNE_STAMP = "prune.stamp"
PRUNE_SECONDS = 3600
RETENTION_SECONDS = 86400  # Owned files older than this are pruned.
LIVE = {"working", "attention"}
# The v1 record keys, in order. The writer emits exactly these.
FIELDS = ("schemaVersion", "eventID", "producerID", "harness", "accountID", "sessionID", "runID",
          "parentSessionID", "agentID", "state", "attentionKind", "outcomeSource", "observedAt",
          "leaseExpiresAt")


def timestamp(now: dt.datetime) -> str:
    return now.astimezone(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def uuid7(now: dt.datetime, previous: object = None) -> str:
    # RFC 9562: 48-bit millisecond timestamp, version 7, variant 10, random tail.
    bits = int(now.timestamp() * 1000) << 80 | (uuid.uuid4().int & ((1 << 80) - 1))
    value = (bits & ~(15 << 76) & ~(3 << 62)) | 7 << 76 | 2 << 62
    prior = native_id(previous)
    if prior and uuid.UUID(prior).version == 7:
        old = uuid.UUID(prior).int
        if old >> 80 == value >> 80:
            value = max(value, old + 1)
    return str(uuid.UUID(int=value))


def native_id(value: object) -> str | None:
    """Only UUID identities cross the boundary; free-form IDs never become content."""
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def account_id(value: object, harness: str) -> bool:
    provider = {"codex": "openai", "claude": "anthropic"}.get(harness)
    return bool(provider and isinstance(value, str) and re.fullmatch(rf"{provider}-[0-9a-f]{{1,64}}", value))


def directory(path: Path) -> Path:
    path = path.expanduser()
    if not path.is_absolute():
        raise ValueError("activity directory must be absolute")
    # Reject redirection at every existing component, including the store root.
    for parent in (*reversed(path.parents), path):
        if parent.is_symlink():
            raise ValueError("activity directory contains a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError("activity destination is not a directory")
    return path


def atomic_json(path: Path, data: dict) -> None:
    encoded = json.dumps(data, separators=(",", ":")).encode()
    if len(encoded) > 1024:
        raise ValueError("activity record exceeds 1 KB")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".activity-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024:
            raise ValueError("invalid activity record")
        value = json.loads(os.read(fd, 1025))
        if not isinstance(value, dict):
            raise ValueError("invalid activity object")
        return value
    finally:
        os.close(fd)


def private_root(env: Mapping[str, str]) -> Path:
    return Path(env.get("CODE_HOME") or Path.home() / ".code").expanduser()


class ActivityStore:
    """Private producer state and its inbox, both under CODE_HOME and never shared."""
    def __init__(self, private: Path):
        self.private = directory(private / STATE)
        self.events = directory(self.private / INBOX)
        for owned in (self.private, self.events):
            owned.chmod(0o700)

    @staticmethod
    def key(account: str, harness: str, session: str) -> str:
        return hashlib.sha256(f"{account}:{harness}:{session}".encode()).hexdigest()

    def emit(self, account: str, harness: str, session: object, state: str, source: str,
             *, run: object = None, parent: object = None, agent: object = None,
             attention: str | None = None, start: bool = False,
             renew_only: bool = False,
             now: dt.datetime | None = None) -> dict | None:
        session = native_id(session)
        if not session or not account_id(account, harness) or state not in STATES or source not in SOURCES:
            return None
        if attention not in (None, "approval", "input"):
            return None
        key = self.key(account, harness, session)
        fd = os.open(self.private / f"{key[:2]}.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("activity lock is not a regular file")
            fcntl.flock(fd, fcntl.LOCK_EX)
            now = now or dt.datetime.now(dt.timezone.utc)
            path = self.private / f"{key}.json"
            try:
                previous = read_json(path)
            except FileNotFoundError:
                previous = {}
            old_state = previous.get("state")
            if state == "quiet" and old_state in TERMINAL | {"quiet"}:
                return None
            if renew_only and old_state in TERMINAL:
                # Hook execution may still read busy after Stop. Never reopen it.
                return None
            run_id = native_id(run)
            if not run_id:
                run_id = str(uuid.uuid4()) if start or not previous else previous.get("runID")
            if not native_id(run_id):
                return None
            if state in TERMINAL and not previous and not native_id(run):
                return None  # An end without a start is not a known run.
            if state in TERMINAL and old_state == state and previous.get("runID") == run_id:
                return None
            values = {"schemaVersion": 1, "eventID": uuid7(now, previous.get("eventID")), "producerID": PRODUCER,
                      "harness": harness, "accountID": account, "sessionID": session,
                      "runID": run_id, "parentSessionID": native_id(parent) or native_id(previous.get("parentSessionID")),
                      "agentID": native_id(agent) or native_id(previous.get("agentID")), "state": state,
                      "attentionKind": attention if state == "attention" else None,
                      "outcomeSource": source, "observedAt": timestamp(now),
                      "leaseExpiresAt": timestamp(now + dt.timedelta(seconds=LEASE_SECONDS))}
            record = {name: values[name] for name in FIELDS}
            atomic_json(self.events / f"{PRODUCER}-{record['eventID']}.json", record)
            atomic_json(path, record)
            return record
        finally:
            os.close(fd)

    def prune_if_due(self, now: dt.datetime | None = None) -> bool:
        """Prune at most once per PRUNE_SECONDS, keyed on a private stamp's mtime."""
        now = now or dt.datetime.now(dt.timezone.utc)
        path = self.private / PRUNE_STAMP
        flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd, existed = os.open(path, flags), True
        except FileNotFoundError:
            try:
                fd, existed = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600), False
            except FileExistsError:
                return False  # Another hook created it first and is pruning.
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("activity prune stamp is not a regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            # A stamp up to a window ahead (a clock moved back) still counts as
            # recent; one further out cannot hold pruning off indefinitely.
            if existed and abs(now.timestamp() - os.fstat(fd).st_mtime) < PRUNE_SECONDS:
                return False
            os.fchmod(fd, 0o600)
            os.utime(fd, (now.timestamp(), now.timestamp()))
            self.prune(now)
            return True
        finally:
            os.close(fd)

    def prune(self, now: dt.datetime | None = None) -> None:
        now = now or dt.datetime.now(dt.timezone.utc)
        for path in self.events.iterdir():
            if not OWN_FILE.fullmatch(path.name) or path.is_symlink():
                continue
            try:
                record = read_json(path)
                observed = dt.datetime.fromisoformat(record["observedAt"].replace("Z", "+00:00"))
                if record.get("producerID") == PRODUCER and now - observed >= dt.timedelta(seconds=RETENTION_SECONDS):
                    path.unlink()
            except (OSError, ValueError, KeyError, TypeError):
                continue
        for path in self.private.glob("*.json"):
            if not re.fullmatch(r"[0-9a-f]{64}[.]json", path.name) or path.is_symlink():
                continue
            fd = None
            try:
                fd = os.open(self.private / f"{path.stem[:2]}.lock", os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                record = read_json(path)
                observed = dt.datetime.fromisoformat(record["observedAt"].replace("Z", "+00:00"))
                if record.get("producerID") == PRODUCER and now - observed >= dt.timedelta(seconds=RETENTION_SECONDS):
                    path.unlink()
            except (OSError, ValueError, KeyError, TypeError):
                continue
            finally:
                if fd is not None:
                    os.close(fd)


def moment(value: object) -> dt.datetime | None:
    """A UTC writer timestamp, or None for anything else."""
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def inbox_records(inbox: Path) -> tuple[list[dict], dict[str, int]]:
    """Valid v1 records plus file counts. File content never leaves this function."""
    records, counts = [], {"ignored": 0, "malformed": 0}
    for path in inbox.iterdir():
        if not OWN_FILE.fullmatch(path.name) or path.is_symlink():
            counts["ignored"] += 1
            continue
        try:
            record = read_json(path)
        except (OSError, ValueError):
            counts["malformed"] += 1
            continue
        if record.get("producerID") != PRODUCER:
            counts["ignored"] += 1
        elif (tuple(record) != FIELDS or type(record["schemaVersion"]) is not int
              or record["schemaVersion"] != 1 or record["harness"] not in ("codex", "claude")
              or not account_id(record["accountID"], record["harness"]) or record["state"] not in STATES
              or not all(native_id(record[name]) for name in ("eventID", "sessionID", "runID"))
              or not moment(record["observedAt"]) or not moment(record["leaseExpiresAt"])):
            counts["malformed"] += 1
        else:
            records.append(record)
    return records, counts


def renewal_verdict(counts: dict) -> str:
    if counts["lapses"] or counts["leaseMismatches"]:
        return "fail"
    return "pass" if counts["qualifyingRuns"] else "inconclusive"


def renewal_report(records: list[dict], now: dt.datetime, window: int, harnesses: tuple[str, ...]) -> dict:
    """Count-only proof that live leases were renewed before they lapsed. No IDs leave it."""
    start, lease = now - dt.timedelta(seconds=window), dt.timedelta(seconds=LEASE_SECONDS)
    runs: dict[tuple, list] = {}
    seen, out_of_window = set(), 0
    for record in records:
        observed = moment(record["observedAt"])
        if not start <= observed <= now:
            out_of_window += 1
            continue
        if record["harness"] not in harnesses or record["eventID"] in seen:
            continue
        seen.add(record["eventID"])
        key = (record["harness"], record["accountID"], record["sessionID"], record["runID"])
        runs.setdefault(key, []).append((observed, record["eventID"], moment(record["leaseExpiresAt"]),
                                         record["state"] in LIVE))
    report = {harness: {"runs": 0, "liveRecords": 0, "qualifyingRuns": 0, "lapses": 0, "leaseMismatches": 0,
                        "maxGapSeconds": 0.0, "maxLiveSpanSeconds": 0.0, "expiredOpen": 0}
              for harness in harnesses}
    for key, events in runs.items():
        counts = report[key[0]]
        counts["runs"] += 1
        events.sort(key=lambda event: event[:2])
        previous = chain_start = None
        qualifying = False
        for observed, event, expires, live in events:
            if expires - observed != lease:
                counts["leaseMismatches"] += 1
            if previous and previous[3]:
                gap = (observed - previous[0]).total_seconds()
                counts["maxGapSeconds"] = max(counts["maxGapSeconds"], round(gap, 3))
                if observed >= previous[2]:
                    counts["lapses"] += 1
                    chain_start = None
            if live:
                counts["liveRecords"] += 1
                chain_start = chain_start or observed
                span = (observed - chain_start).total_seconds()
                counts["maxLiveSpanSeconds"] = max(counts["maxLiveSpanSeconds"], round(span, 3))
                qualifying = qualifying or span > LEASE_SECONDS
            else:
                chain_start = None
            previous = (observed, event, expires, live)
        counts["qualifyingRuns"] += qualifying
        if previous[3] and previous[2] <= now:
            counts["expiredOpen"] += 1  # Still open at check time; lapse is not provable yet.
    for counts in report.values():
        counts["verdict"] = renewal_verdict(counts)
    verdicts = {counts["verdict"] for counts in report.values()}
    overall = "fail" if "fail" in verdicts else "pass" if verdicts == {"pass"} else "inconclusive"
    return {"verdict": overall, "leaseSeconds": LEASE_SECONDS, "windowSeconds": window,
            "harnesses": report, "files": {"outOfWindow": out_of_window}}


def home_key(harness: str, env: dict) -> str:
    key = "CODEX_HOME" if harness == "codex" else "CLAUDE_CONFIG_DIR"
    default = ".codex" if harness == "codex" else ".claude"
    home = Path(env.get(key) or Path.home() / default).expanduser().resolve()
    return hashlib.sha256(f"{harness}:{home}".encode()).hexdigest()


def hook_activity(payload: object, env: dict) -> None:
    if not isinstance(payload, dict) or env.get("AGENT_ACTIVITY_DISABLED") == "1":
        return
    harness = env.get("CODEX_SKILLS_HARNESS", "claude")
    if harness not in ("codex", "claude"):
        return
    event = payload.get("hook_event_name")
    events = {"UserPromptSubmit": "working", "Stop": "quiet", "StopFailure": "failed",
              "Interrupt": "cancelled", "Notification": "attention", "PermissionRequest": "attention",
              "SubagentStart": "working", "SubagentStop": "quiet", "SessionEnd": "quiet"}
    if event not in events:
        return
    private = private_root(env)
    try:
        binding = read_json(private / STATE / f"binding-{home_key(harness, env)}.json")
    except FileNotFoundError:
        return  # Observer has not mapped this home to the launch-receipt account.
    # A legacy binding's storageRoot is ignored: hooks write only the private inbox.
    if not account_id(binding.get("accountID"), harness):
        return
    # Codex outcomes come from the daemon, avoiding hook/observer duplicate runs.
    if harness == "codex":
        return
    attention = None
    if event == "Notification":
        attention = {"permission_prompt": "approval", "elicitation_dialog": "input"}.get(payload.get("notification_type"))
        if attention is None:
            return
    elif event == "PermissionRequest":
        attention = "approval"
    session = native_id(payload.get("session_id"))
    if not session:
        return
    parent = agent = None
    raw_agent = payload.get("agent_id")
    if raw_agent is not None or event in ("SubagentStart", "SubagentStop"):
        if not isinstance(raw_agent, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", raw_agent):
            return
        parent = session
        agent = str(uuid.uuid5(uuid.UUID(session), raw_agent))
        session = agent
    store = ActivityStore(private)
    # Stop/SubagentStop can be continued by another hook. Even later idle cannot distinguish
    # completion from an interrupted continuation; native background done can.
    store.emit(binding["accountID"], harness, session, events[event], "claude-hook",
               parent=parent, agent=agent, attention=attention,
               start=event in ("UserPromptSubmit", "SubagentStart"))
    # Hooks keep private state bounded without the observer.
    store.prune_if_due()
