#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Record that a daily direction turn just finished, on this machine and for every machine.

A daily turn is account-wide: it always covers the Director's OWNER/direction,
whatever folder the session started in. The turn goes into the local marker
and into one shared turn record, an issue in OWNER/direction kept by the
automation identity. The session-start hook in `hooks/direction_check_hook.py`
reads the marker, reads the shared record when the marker's turn is stale,
and reminds the owner while a turn is overdue. Only the direction agent runs
this, at the end of a turn the owner took part in. Audits are not marked by
hand: `direction_audit.py` records each repository's audit and, after a
complete `--weekly` run, the weekly audit, locally and in its own shared
record. The `audits` map holds where each repository's next audit window
starts; `audit_runs` holds when an audit last ran and, when it kept its window
open, why; `weekly_audit` holds when the last complete weekly audit ran.
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
WEEKLY_RECORD_TAG = "<!-- direction-weekly-audit-record -->"
WEEKLY_RECORD_TITLE = "Weekly direction audits"
OVERALL_REPO = "direction"
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
    if not isinstance(current.get("audit_runs"), dict):
        current.pop("audit_runs", None)
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


def account(value: str) -> str:
    """The Director's account from OWNER, or from OWNER/REPO for the older --repo form."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?", value):
        raise ValueError("the account must be OWNER (or OWNER/REPO)")
    return value.split("/", 1)[0]


def legacy_repo(value: str) -> str:
    """The older `turn --repo OWNER/REPO` form keeps working; only its account is used."""
    if "/" not in value:
        raise ValueError("--repo takes OWNER/REPO; pass --owner OWNER instead")
    return account(value)


def direction_repo(value: str) -> str:
    """OWNER/direction: the repository a daily turn and a weekly audit always cover."""
    return f"{account(value)}/{OVERALL_REPO}"


def mark_turn(path: Path, owner: str, now: dt.datetime) -> dict[str, object]:
    """Record an account-wide daily turn; its coverage is always OWNER/direction."""
    repo = direction_repo(owner)
    with marker_lock(path) as path:
        current = load(path)
        current["turn"] = utc_stamp(now)
        current["turn_repo"] = repo
        save(path, current)
    return current


def mark_weekly(path: Path, owner: str, now: dt.datetime) -> dict[str, object]:
    """Record a complete weekly audit of the account; only `direction_audit.py --weekly` calls this."""
    record = direction_repo(owner)
    with marker_lock(path) as path:
        current = load(path)
        current["weekly_audit"] = {"ran": utc_stamp(now), "record": record}
        save(path, current)
    return current


def mark_audit(path: Path, repo: str, now: dt.datetime, held: str | None = None) -> dict[str, object]:
    """Record this repository's audit without changing daily-turn evidence.

    A held audit ran but keeps its window open for a rerun: it records the run
    and the reason, and leaves the window start in `audits` where it was.
    """
    with marker_lock(path) as path:
        current = load(path)
        stamp = utc_stamp(now)
        audits, runs = current["audits"], current.setdefault("audit_runs", {})
        assert isinstance(audits, dict) and isinstance(runs, dict)
        if held is None:
            audits[repo] = stamp
            runs[repo] = {"ran": stamp}
        else:
            runs[repo] = {"ran": stamp, "held": held}
        save(path, current)
    return current


def drop_audit(path: Path, repo: str) -> dict[str, object]:
    """Remove a repository an audit confirmed archived from the weekly maps."""
    with marker_lock(path) as path:
        current = load(path)
        audits, runs = current["audits"], current.get("audit_runs", {})
        assert isinstance(audits, dict) and isinstance(runs, dict)
        if repo in audits or repo in runs:
            audits.pop(repo, None)
            runs.pop(repo, None)
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


def weekly_record_body(stamp: str) -> str:
    """The shared weekly-audit record the hook parses; its tag and line format are the contract."""
    return (
        f"{WEEKLY_RECORD_TAG}\n"
        "`direction_audit.py --weekly` updates this issue after a weekly audit that read every adopted repository, "
        "so the session-start reminder on each of the Director's machines sees the same last audit. "
        "Keep it open and do not edit it by hand.\n\n"
        f"Last weekly audit: {stamp}\n"
    )


def run_helper(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, input=stdin, text=True, capture_output=True, timeout=SHARED_TIMEOUT)


def failure(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}").splitlines()[-1][:300]


def share_turn(stamp: str, repo: str) -> dict[str, object]:
    """Keep the shared turn record in OWNER/direction current, through the automation helpers."""
    return share_record(direction_repo(repo), TURN_RECORD_TAG, TURN_RECORD_TITLE, turn_record_body(stamp, direction_repo(repo)))


def share_weekly(stamp: str, owner: str) -> dict[str, object]:
    """Keep the shared weekly-audit record in OWNER/direction current; the turn record is not touched."""
    return share_record(direction_repo(owner), WEEKLY_RECORD_TAG, WEEKLY_RECORD_TITLE, weekly_record_body(stamp))


def share_record(record_repo: str, tag: str, title: str, body: str) -> dict[str, object]:
    """Find the lowest-numbered open issue carrying `tag`, then rewrite it, or create one."""
    try:
        listed = run_helper([str(GITHUB_SCRIPTS / "gh-with-env-token"), "api",
                             f"repos/{record_repo}/issues?state=open&sort=created&direction=asc&per_page=100", "--method", "GET"])
        if listed.returncode != 0:
            return {"ok": False, "repo": record_repo, "error": f"could not list issues: {failure(listed)}"}
        issues = json.loads(listed.stdout)
        records = sorted(
            issue["number"] for issue in issues
            if isinstance(issue, dict) and "pull_request" not in issue
            and tag in str(issue.get("body") or "")
        )
        issue_helper = str(GITHUB_SCRIPTS / "gh-issue")
        if records:
            written = run_helper([issue_helper, "edit", str(records[0]), "--repo", record_repo, "--body-file", "-"], body)
        else:
            written = run_helper([issue_helper, "create", title, "--repo", record_repo], body)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        return {"ok": False, "repo": record_repo, "error": str(exc)[:300]}
    if written.returncode != 0:
        return {"ok": False, "repo": record_repo, "error": failure(written)}
    return {"ok": True, "repo": record_repo, "issue": records[0] if records else "created"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["turn"], help="only a daily turn can be marked by hand")
    coverage = parser.add_mutually_exclusive_group(required=True)
    coverage.add_argument("--owner", type=account, help="the Director's account; the turn covers OWNER/direction")
    coverage.add_argument("--repo", type=legacy_repo, dest="owner", metavar="OWNER/REPO",
                          help="older form: OWNER/REPO, recorded as OWNER/direction")
    args = parser.parse_args(argv)
    path: Path | str = os.environ.get("DIRECTION_MARKER") or "default direction marker"
    try:
        path = marker_path()
        written = mark_turn(path, args.owner, dt.datetime.now(dt.timezone.utc))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        error = f"Could not record daily direction marker at {path}: {exc}"
        print(json.dumps({"ok": False, "marker": str(path), "local_recorded": False, "error": error}))
        print(f"ERROR: {error}. The turn was not recorded locally or shared.", file=sys.stderr)
        return 1
    shared = share_turn(str(written["turn"]), args.owner)
    if not shared["ok"]:
        print(f"ERROR: Daily turn recorded at {path}, but the shared record failed: {shared['error']}", file=sys.stderr)
    print(json.dumps({"ok": shared["ok"], "marker": str(path), "turn": written["turn"], "turn_repo": written["turn_repo"], "shared": shared}))
    # The local turn is written either way; a failed shared write leaves other machines reminding.
    return 0 if shared["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
