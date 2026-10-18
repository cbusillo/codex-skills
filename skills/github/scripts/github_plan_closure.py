# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Host-local closure evidence. A receipt alone never proves an idle thread ended."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys


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
    return {"host": socket.gethostname(), "boot": boot, "uid": os.getuid()}


def transcript_identity(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def transcripts(session: str, primary: str) -> dict:
    from github_plan_sessions import codex_homes, native_helpers
    paths = {Path(primary).resolve(strict=True)}
    for home in codex_homes(strict=True):
        for root in (home / "sessions", home / "archived_sessions"):
            if root.exists():
                paths.update(p.resolve(strict=True) for p in root.rglob(f"*{session}*.jsonl"))
    snapshots = {}
    for path in sorted(paths):
        before = path.stat()
        data = path.read_bytes()
        records = native_helpers()[1].read_jsonl(path)
        status = native_helpers()[1].session_status(
            {"session_id": session, "harness": "codex", "transcript": str(path)})
        after = path.stat()
        if transcript_identity(before) != transcript_identity(after) or not status["safe_verdict"]:
            raise ValueError("native transcript is changing or lacks a final safe verdict")
        timestamps = [r["timestamp"] for r in records if r.get("timestamp")]
        if not timestamps:
            raise ValueError("native transcript lacks timestamped evidence")
        from github_plan_release import stamp
        latest = max(timestamps, key=stamp)
        snapshots[str(path)] = {"identity": transcript_identity(after),
                                "sha256": hashlib.sha256(data).hexdigest(), "latest": latest}
    return snapshots


def evidence(entry: dict) -> dict:
    from github_plan_sessions import codex_homes
    if not Path(entry["codex_home"]).expanduser().is_absolute() or not Path(entry["transcript"]).is_absolute():
        raise ValueError("closure identity paths must be absolute")
    home = Path(entry["codex_home"]).expanduser().resolve(strict=True)
    primary = Path(entry["transcript"]).resolve(strict=True)
    homes = codex_homes(strict=True)
    if home not in homes or not any(
        primary.is_relative_to((home / root).resolve()) for root in ("sessions", "archived_sessions")
    ):
        raise ValueError("closure account home and transcript identity disagree")
    snapshots = transcripts(entry["session_id"], str(primary))
    from github_plan_release import stamp
    if any(stamp(snapshot["latest"]) > stamp(entry["ended_at"]) for snapshot in snapshots.values()):
        raise ValueError("native transcript activity is newer than the closure")
    return {"host": host_identity(), "codex_home": str(home),
            "homes": [str(h) for h in homes], "transcripts": snapshots}


def verified_record(session: str, home: Path) -> dict | None:
    """Optional, read-only proof; unavailable evidence never changes native liveness."""
    try:
        path = closed_log()
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o077):
            return None
        rows = json.loads(path.read_text())
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            return None
        matching = [r for r in rows if r.get("session_id") == session]
        if len(matching) != 1:
            return None
        entry = {k: v for k, v in matching[0].items() if not k.startswith("_")}
        proof = entry.get("closure_evidence")
        if (entry.get("harness") != "codex" or not isinstance(proof, dict)
                or proof.get("codex_home") != str(home.resolve())):
            return None
        if isinstance(entry.get("tty"), str):
            entry["tty"] = entry["tty"].removeprefix("/dev/")
        from github_plan_sessions import codex_homes, native_helpers
        native_helpers()
        import release_session_claims
        roots = [str(h / root) for h in codex_homes(strict=True) for root in ("sessions", "archived_sessions")]
        release_session_claims.validate_closure({**entry, "_transcript_roots": roots}, "claim-observer")
        if evidence(entry) != entry["closure_evidence"]:
            return None
        after = path.lstat()
        if (before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_ino, after.st_mtime_ns, after.st_size):
            return None
        return entry
    except (OSError, ValueError, KeyError, TypeError, ImportError, subprocess.SubprocessError):
        return None
