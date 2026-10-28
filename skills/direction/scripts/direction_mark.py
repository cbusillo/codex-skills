#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
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
OVERALL_REPO = "direction"
SHARED_TIMEOUT = 60
# Pages of open issues read to find a record. A record not found within them is unconfirmed.
RECORD_PAGES = 10
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


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
    return mark_weekly_audits(path, owner, now, [], True)


def _apply_audit_update(current: dict[str, object], update: dict[str, object]) -> bool:
    audits, runs = current["audits"], current.get("audit_runs", {})
    assert isinstance(audits, dict) and isinstance(runs, dict)
    repo = str(update["repo"])
    if update["action"] == "drop":
        changed = repo in audits or repo in runs
        audits.pop(repo, None)
        runs.pop(repo, None)
        return changed
    current["audit_runs"] = runs
    stamp = str(update["stamp"])
    held = update.get("held")
    if held is None:
        audits[repo] = stamp
        runs[repo] = {"ran": stamp}
    else:
        runs[repo] = {"ran": stamp, "held": held}
        since = update.get("since")
        if since is not None:
            audits.setdefault(repo, str(since))
    return True


def mark_weekly_audits(path: Path, owner: str, now: dt.datetime,
                       updates: list[dict[str, object]], complete: bool) -> dict[str, object]:
    with marker_lock(path) as path:
        current = load(path)
        for update in updates:
            _apply_audit_update(current, update)
        if complete:
            current["weekly_audit"] = {"ran": utc_stamp(now), "record": direction_repo(owner)}
        save(path, current)
    return current


def mark_audit(path: Path, repo: str, now: dt.datetime, held: str | None = None,
               since: dt.datetime | None = None) -> dict[str, object]:
    with marker_lock(path) as path:
        current = load(path)
        _apply_audit_update(current, {"repo": repo, "action": "record", "stamp": utc_stamp(now),
                                      "held": held, "since": utc_stamp(since) if since is not None else None})
        save(path, current)
    return current


def drop_audit(path: Path, repo: str) -> dict[str, object]:
    with marker_lock(path) as path:
        current = load(path)
        if _apply_audit_update(current, {"repo": repo, "action": "drop"}):
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
    return share_record(direction_repo(repo), TURN_RECORD_TAG, TURN_RECORD_TITLE, turn_record_body(stamp, direction_repo(repo)))


def trusted_record(issue: object, tag: str) -> bool:
    """An open issue carrying `tag`, written by a repository member or a bot, as the hook trusts."""
    if not isinstance(issue, dict) or "pull_request" in issue or tag not in str(issue.get("body") or ""):
        return False
    user = issue.get("user")
    return issue.get("author_association") in TRUSTED_ASSOCIATIONS or (isinstance(user, dict) and user.get("type") == "Bot")


def oldest_record(record_repo: str, tag: str) -> tuple[int | None, str | None]:
    """The lowest-numbered trusted open issue carrying `tag`, or None and why the search did not finish.

    Issues are listed oldest first, so the first record found is the oldest. Not finding
    one is confirmed only when every open issue was read.
    """
    for page in range(1, RECORD_PAGES + 1):
        listed = run_helper([str(GITHUB_SCRIPTS / "gh-with-env-token"), "api",
                             f"repos/{record_repo}/issues?state=open&sort=created&direction=asc&per_page=100&page={page}",
                             "--method", "GET"])
        if listed.returncode != 0:
            return None, f"could not list issues: {failure(listed)}"
        issues = json.loads(listed.stdout)
        if not isinstance(issues, list):
            return None, "could not list issues: GitHub returned something other than an issue list"
        records = sorted(issue["number"] for issue in issues if trusted_record(issue, tag))
        if records:
            return records[0], None
        if len(issues) < 100:
            return None, None
    return None, f"no record in the first {RECORD_PAGES * 100} open issues and more exist; not creating another"


def share_record(record_repo: str, tag: str, title: str, body: str) -> dict[str, object]:
    """Rewrite the oldest trusted open issue carrying `tag`, or create one when a full search found none."""
    try:
        number, error = oldest_record(record_repo, tag)
        if error:
            return {"ok": False, "repo": record_repo, "error": error}
        issue_helper = str(GITHUB_SCRIPTS / "gh-issue")
        if number is not None:
            written = run_helper([issue_helper, "edit", str(number), "--repo", record_repo, "--body-file", "-"], body)
        else:
            written = run_helper([issue_helper, "create", title, "--repo", record_repo], body)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as exc:
        return {"ok": False, "repo": record_repo, "error": str(exc)[:300]}
    if written.returncode != 0:
        return {"ok": False, "repo": record_repo, "error": failure(written)}
    return {"ok": True, "repo": record_repo, "issue": number if number is not None else "created"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record an account-wide daily direction turn locally and publish its shared record.")
    parser.add_argument("kind", choices=["turn"], help="only a daily turn can be marked by hand")
    coverage = parser.add_mutually_exclusive_group(required=True)
    coverage.add_argument("--owner", type=account, help="the Director's account; the turn covers OWNER/direction")
    coverage.add_argument("--repo", type=legacy_repo, dest="owner", metavar="OWNER/REPO",
                          help="older form: OWNER/REPO, recorded as OWNER/direction")
    args = parser.parse_args(argv)
    shown = os.environ.get("DIRECTION_MARKER") or "default direction marker"
    now = dt.datetime.now(dt.timezone.utc)
    try:
        path = marker_path()
        shown = str(path)
        written = mark_turn(path, args.owner, now)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        error = f"Could not record daily direction marker at {shown}: {exc}"
        print(json.dumps({"ok": False, "marker": shown, "local_recorded": False, "error": error}))
        print(f"ERROR: {error}. The turn was not recorded locally or shared.", file=sys.stderr)
        return 1
    shared = share_turn(utc_stamp(now), args.owner)
    if not shared["ok"]:
        print(f"ERROR: Daily turn recorded at {path}, but the shared record failed: {shared['error']}", file=sys.stderr)
    print(json.dumps({"ok": shared["ok"], "marker": str(path), "turn": written["turn"], "turn_repo": written["turn_repo"], "shared": shared}))
    # The local turn is written either way; a failed shared write leaves other machines reminding.
    return 0 if shared["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
