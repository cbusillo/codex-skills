#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Record that a daily direction turn just finished, on this machine and for every machine.

The turn goes into the local marker and into one shared turn record, an issue
in OWNER/direction kept by the automation identity, where OWNER owns the
covered repository. The session-start hook in `hooks/direction_check_hook.py`
reads the marker, reads the shared record when the marker's turn is stale,
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
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

MARKER_NAME = "direction-last-check.json"
GITHUB_SCRIPTS = Path(__file__).resolve().parents[2] / "github" / "scripts"
TURN_RECORD_TAG = "<!-- direction-turn-record -->"
TURN_RECORD_TITLE = "Daily direction turns"
SHARED_TIMEOUT = 60


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


def covered_repo(repo: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("daily coverage must be OWNER/REPO")
    return repo


def mark_turn(path: Path, repo: str, now: dt.datetime) -> dict[str, object]:
    repo = covered_repo(repo)
    with marker_lock(path) as path:
        current = load(path)
        current["turn"] = utc_stamp(now)
        current["turn_repo"] = repo
        save(path, current)
    return current


def mark_audit(path: Path, repo: str, now: dt.datetime) -> dict[str, object]:
    """Record this repository's audit without changing daily-turn evidence."""
    with marker_lock(path) as path:
        current = load(path)
        stamp = utc_stamp(now)
        audits = current["audits"]
        assert isinstance(audits, dict)
        audits[repo] = stamp
        save(path, current)
    return current


def turn_record_body(stamp: str, repo: str) -> str:
    """The shared record the hook parses; its tag and line format are the contract."""
    return (
        f"{TURN_RECORD_TAG}\n"
        "The `direction` skill updates this issue at the end of every daily turn, so the session-start "
        "reminder on each of the Director's machines sees the same last turn. Keep it open and do not edit it by hand.\n\n"
        f"Last daily turn: {stamp} covering {repo}\n"
    )


def run_helper(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, input=stdin, text=True, capture_output=True, timeout=SHARED_TIMEOUT)


def failure(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}").splitlines()[-1][:300]


def share_turn(stamp: str, repo: str) -> dict[str, object]:
    """Keep the shared turn record in OWNER/direction current, through the automation helpers."""
    record_repo = f"{repo.split('/', 1)[0]}/direction"
    try:
        listed = run_helper([str(GITHUB_SCRIPTS / "gh-with-env-token"), "api",
                             f"repos/{record_repo}/issues?state=open&per_page=100", "--method", "GET"])
        if listed.returncode != 0:
            return {"ok": False, "repo": record_repo, "error": f"could not list issues: {failure(listed)}"}
        issues = json.loads(listed.stdout)
        records = sorted(
            issue["number"] for issue in issues
            if isinstance(issue, dict) and "pull_request" not in issue
            and TURN_RECORD_TAG in str(issue.get("body") or "")
        )
        body = turn_record_body(stamp, repo)
        issue_helper = str(GITHUB_SCRIPTS / "gh-issue")
        if records:
            written = run_helper([issue_helper, "edit", str(records[0]), "--repo", record_repo, "--body-file", "-"], body)
        else:
            written = run_helper([issue_helper, "create", TURN_RECORD_TITLE, "--repo", record_repo], body)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        return {"ok": False, "repo": record_repo, "error": str(exc)[:300]}
    if written.returncode != 0:
        return {"ok": False, "repo": record_repo, "error": failure(written)}
    return {"ok": True, "repo": record_repo, "issue": records[0] if records else "created"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["turn"], help="only a daily turn can be marked by hand")
    parser.add_argument("--repo", required=True, type=covered_repo, help="OWNER/REPO covered by the daily turn")
    args = parser.parse_args(argv)
    path = marker_path()
    written = mark_turn(path, args.repo, dt.datetime.now(dt.timezone.utc))
    shared = share_turn(str(written["turn"]), args.repo)
    print(json.dumps({"ok": shared["ok"], "marker": str(path), "turn": written["turn"], "turn_repo": written["turn_repo"], "shared": shared}))
    # The local turn is written either way; a failed shared write leaves other machines reminding.
    return 0 if shared["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
