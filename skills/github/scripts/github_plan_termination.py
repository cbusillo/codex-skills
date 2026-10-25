# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5", "tree-sitter==0.26.0", "tree-sitter-bash==0.25.1", "tree-sitter-javascript==0.25.0"]
# ///
"""Read-only source-host checks for claims interrupted by a host/terminal crash."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import socket
import subprocess
from subprocess import run as run_terminal
import sys
import time
from typing import NamedTuple

def native_helpers():
    # GitHub reads and historical receipts do not load terminal dependencies.
    directory = str(Path(__file__).resolve().parents[2] / "supervisor/scripts")
    if directory not in sys.path:
        sys.path.append(directory)
    import session_record
    import session_liveness
    return session_record, session_liveness


def inventory():
    return native_helpers()[1].inventory()


MARKER = "github-plan:termination "
QUIET_PERIOD = timedelta(minutes=5)


def stamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Termination evidence requires timezone-qualified timestamps")
    return result


def _validate_metadata(data, session):
    if not isinstance(data, dict) or data.get("claim_session", data.get("session_id")) != session:
        raise ValueError("Termination evidence must identify the exact native session")
    if not data.get("exit_record") and (type(data.get("pid")) is not int or data["pid"] <= 0):
        raise ValueError("Termination evidence requires the recorded source PID")
    if not isinstance(data.get("host"), str) or not data["host"]:
        raise ValueError("Termination evidence requires the recorded source host")
    if not isinstance(data.get("cause"), str) or not data["cause"].strip():
        raise ValueError("Termination evidence requires the cause of termination")
    artifacts = data.get("retained_artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("Termination evidence must name retained worktrees and branches")
    for artifact in artifacts:
        if (not isinstance(artifact, dict)
                or not isinstance(artifact.get("branch"), str) or not artifact["branch"].strip()
                or artifact.get("worktree") is not None and (
                    not isinstance(artifact["worktree"], str) or not Path(artifact["worktree"]).is_absolute())):
            raise ValueError("Each retained artifact requires a branch and absolute worktree path")


def validate(data, session, *, apply_policy=True):
    _validate_metadata(data, session)
    if data.get("terminal_state") == "exit recorded":
        if not isinstance(data.get("exit_record"), dict) or data["exit_record"].get("kind") != "codex_command" or not re.fullmatch(r"[0-9a-f]{64}", data.get("exit_digest", "")):
            raise ValueError("Historical termination requires its original exit evidence digest")
    elif data.get("exit_record"):
        raise ValueError("Historical exit cannot substitute for crash terminal evidence")
    ended, checked, last = (stamp(data[key]) for key in ("ended_at", "checked_at", "last_activity_at"))
    if last > ended or ended > checked or apply_policy and checked - last < QUIET_PERIOD:
        raise ValueError("Source transcript has recent activity or activity after termination")
    if (not isinstance(data.get("terminal_state"), str) or not data["terminal_state"]
            or apply_policy and data["terminal_state"] not in {"pane dead", "server missing", "server replaced", "exit recorded"}):
        raise ValueError("Termination evidence requires a dead pane or missing server")
    return data


def evidence(text, session, *, apply_policy=True):
    lines = text.splitlines()
    if lines[:1] != [f"Terminated session {session}"]:
        return None
    records = [line for line in lines if line.startswith("<!-- " + MARKER) and line.endswith(" -->")]
    if len(records) != 1:
        raise ValueError("Terminated-session attestation requires one termination evidence record")
    data = json.loads(records[0][len("<!-- " + MARKER):-len(" -->")])
    return validate(data, session, apply_policy=apply_policy)


def terminal_state(record):
    server, identity = record.get("tmux_socket"), record.get("tmux_session_id")
    if not isinstance(server, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", server):
        raise ValueError("Termination record requires its original tmux socket name")
    match = re.fullmatch(rf"tmux:{re.escape(server)}:([0-9a-f]{{32}})", identity or "")
    if not match:
        raise ValueError("Termination record requires the original nonce-bearing tmux session ID")
    result = run_terminal(["tmux", "-L", server, "list-panes", "-a", "-F",
                           "#{@supervisor_identity}\t#{pane_dead}\t#{start_time}"],
                          capture_output=True, text=True, timeout=10, check=False)
    if result.returncode:
        error = result.stderr.strip()
        if result.returncode == 1 and (
            error.startswith("no server running on ")
            or error.startswith("error connecting to ") and error.endswith("(No such file or directory)")
        ):
            return "server missing"
        raise ValueError("Cannot verify the original tmux server; preserve claims")
    rows = [line.split("\t") for line in result.stdout.splitlines()]
    if not rows or any(len(row) != 3 or row[1] not in {"0", "1"} or not row[2].isdigit() for row in rows):
        raise ValueError("Original tmux server inventory is uncertain")
    matches = [row for row in rows if row[0] == match[1]]
    if len(matches) == 1 and matches[0][1] == "1":
        return "pane dead"
    # A restarted server reuses pane numbers. Its creation time and absence of
    # the recorded launch nonce establish that the original server is gone.
    starts = {int(row[2]) for row in rows}
    if not matches and len(starts) == 1 and starts.pop() > stamp(record["ended_at"]).timestamp():
        return "server replaced"
    raise ValueError("Original tmux pane is live or its identity is uncertain")


def transcript_snapshot(record, path):
    """Keep a crash-truncated final line intact; never tolerate interior corruption."""
    before = path.stat()
    with path.open(encoding="utf-8") as stream:
        lines = list(stream)
    records, truncated = native_transcript_records(record, lines)
    after = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise ValueError("Source transcript changed during termination checks")
    return records, after.st_mtime_ns, after.st_size, truncated


def transcript_records(record, lines, *, require_identity: bool = True):
    reader, _ = native_helpers()
    records, truncated = [], False
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1 and not line.endswith(("\n", "\r")):
                truncated = True
                break
            raise ValueError("Source transcript has corrupt interior or complete records") from None
        if not isinstance(row, dict):
            raise ValueError("Source transcript record is not an object")
        reader.validate_record(row)
        records.append(row)
    identities = ({row.get("payload", {}).get("id") for row in records if row.get("type") == "session_meta"}
                  if record["harness"] == "codex" else
                  {row["sessionId"] for row in records if row.get("sessionId")})
    if require_identity and identities != {record["session_id"]}:
        raise ValueError("Source transcript identity is uncertain")
    return records, truncated


def native_transcript_records(record: dict, lines: list[str]) -> tuple[list[dict], bool]:
    codex = record["harness"] == "codex"
    records, truncated = transcript_records(record, lines, require_identity=not codex)
    if codex:
        native_helpers()
        import claim_history
        records = claim_history.root_records(Path(record["transcript"]), "codex", record["session_id"], records)
    return records, truncated


def require_preboot_action(action: dict, booted_at: datetime) -> None:
    if action.get("action_pending") and stamp(action["action_at"]) >= booted_at:
        raise ValueError("source native action occurred after reboot; preserve claims")


def _transcript_activity(times, mtime_ns, truncated, now, ended):
    if not times or max(times) > ended:
        raise ValueError("Source transcript is uncertain or resumed after termination")
    modified = datetime.fromtimestamp(mtime_ns / 1e9, timezone.utc)
    if truncated and modified > ended:
        raise ValueError("Truncated transcript has activity after termination")
    if now - max(*times, modified) < QUIET_PERIOD:
        raise ValueError("Source transcript activity is recent; preserve claims")
    return max(times)


def verify(record, session, *, deadline: float | None = None):
    """Never infer termination from absence alone; caller supplies original identity."""
    import github_plan_closure
    return _verify(record, session, lambda pid, argv: github_plan_closure.guarded_launch_endpoint(pid, argv) is not None, deadline=deadline)


class _TerminationProcess(NamedTuple):
    original: dict
    physical: dict
    cutoff: datetime | None
    roots: frozenset[Path]
    rebooted_at: datetime | None


def _verify_queue_source(record, session, *, latest_pid=None):
    """Bind original cleanup identity once; never grant claim-release authority."""
    import github_plan_closure
    process = _initial_source(record, session, latest_pid=latest_pid)
    _physical_exit(process, lambda pid, argv: github_plan_closure.guarded_launch_endpoint(pid, argv) is not None)
    terminal_state(record)
    return process


def _physical_exit(process, independent_harness):
    native_helpers()[1].verify_process_exit(process.physical, rows=inventory(), ended_at=process.cutoff,
                                           independent_harness=independent_harness, rebooted_at=process.rebooted_at)


def _recheck_queue_process(process):
    import github_plan_closure
    if process.rebooted_at is not None:
        _verify_transcripts(process.original, process.roots, before_boot=process.rebooted_at)
    _physical_exit(process, lambda pid, argv: github_plan_closure.guarded_launch_endpoint(pid, argv) is not None)


def _initial_source(record, session, *, latest_pid=None):
    _validate_metadata(record, session)
    if record.get("session_id") != session or record.get("host") != socket.gethostname():
        raise ValueError("Run termination checks on the recorded source host with the exact native session")
    if record.get("harness") not in {"codex", "claude"}:
        raise ValueError("Termination record requires the original native harness")
    pid, tty = record.get("pid"), record.get("tty")
    if type(pid) is not int or pid <= 0 or not isinstance(tty, str) or not tty.strip():
        raise ValueError("Termination record requires the recorded source PID and TTY")
    from github_plan_sessions import native_helpers as session_helpers
    session_helpers()
    shared = native_helpers()[1]
    physical = dict(record)
    if latest_pid is not None and latest_pid not in {record.get("pid"), record.get("launch_pid")}:
        if record.get("launch_pid") is not None:
            raise ValueError("multiple known source launch PIDs require matching original context")
        physical["launch_pid"] = latest_pid  # Negative veto only; never supplies native pane authority.
    cutoff = shared.native_exit_cutoff(record)
    ended = stamp(record["ended_at"])
    if ended > datetime.now(timezone.utc) or cutoff is not None and cutoff > ended:
        raise ValueError("Native pane exit or termination time is in the future")
    roots = frozenset(_transcript_roots(record))
    booted = shared.host_boot_time()
    activity = _verify_transcripts(record, roots) if booted is not None and booted <= datetime.now(timezone.utc) else None
    original_launch = latest_pid is None or latest_pid in {record.get("pid"), record.get("launch_pid")}
    rebooted_at = booted if original_launch and activity is not None and activity.latest < booted else None
    if rebooted_at is not None:
        import github_plan_closure
        require_preboot_action(github_plan_closure.lease_state(session), rebooted_at)
    process = _TerminationProcess(dict(record), physical, rebooted_at or cutoff, roots, rebooted_at)
    return process


def _transcript_roots(record):
    roots = record.get("transcript_roots")
    if not isinstance(roots, list) or not roots or not all(isinstance(root, str) and Path(root).is_dir() for root in roots):
        raise ValueError("Supply every known source-account transcript root on the source host")
    from github_plan_sessions import observation_homes
    configured = {home / name for home in observation_homes() for name in ("sessions", "archived_sessions")}
    return {Path(root).resolve(strict=True) for root in [*roots, *(path for path in configured if path.is_dir())]}


def _verify(record, session, independent_harness, *, deadline: float | None = None):
    if deadline is not None and time.monotonic() > deadline:
        raise TimeoutError("termination verification exceeded its deadline")
    if record.get("exit_record"):
        import github_plan_termination_history
        return github_plan_termination_history.verify(record, session)
    import github_plan_closure
    process = _initial_source(record, session)
    action = github_plan_closure.lease_state(session)
    if process.rebooted_at is not None and action.get("action_pending"):
        import asyncio
        import github_plan_native
        asyncio.run(github_plan_native.prove_crash_end(record, deadline=deadline))
    _physical_exit(process, lambda pid, argv: (process.rebooted_at is not None or not github_plan_closure.action_pending(session))
                   and independent_harness(pid, argv))
    activity = _verify_transcripts(record, process.roots, before_boot=process.rebooted_at)
    data = {key: record[key] for key in ("session_id", "pid", "host", "ended_at", "cause", "retained_artifacts", "tmux_session_id")}
    data.update(checked_at=datetime.now(timezone.utc).isoformat(), last_activity_at=activity.last.isoformat(), terminal_state=terminal_state(record))
    if process.rebooted_at is not None:
        data["booted_at"] = process.rebooted_at.isoformat()
    validate(data, session)
    _verify_artifacts(data["retained_artifacts"])
    if process.rebooted_at is not None and github_plan_closure.lease_state(session) != action:
        raise ValueError("source native action changed during termination proof")
    if deadline is not None and time.monotonic() > deadline:
        raise TimeoutError("termination verification exceeded its deadline")
    return data


class _TranscriptActivity(NamedTuple):
    last: datetime
    latest: datetime


def _verify_transcripts(record: dict, roots: frozenset[Path], *, before_boot: datetime | None = None) -> _TranscriptActivity:
    session = record["session_id"]
    paths = {Path(record["transcript"]).resolve()}
    for root in roots:
        paths.update(path.resolve() for path in Path(root).rglob(f"*{session}*.jsonl"))
    now = datetime.now(timezone.utc)
    ended = stamp(record["ended_at"])
    last, latest = None, None
    for path in paths:
        before = transcript_snapshot(record, path)
        records, mtime_ns, _size, truncated = before
        times = [stamp(row["timestamp"]) for row in records if row.get("timestamp")]
        _transcript_activity(times, mtime_ns, truncated, now, ended)
        modified = max(*times, datetime.fromtimestamp(mtime_ns / 1e9, timezone.utc))
        if before_boot is not None and modified >= before_boot:
            raise ValueError("Source transcript has activity since host boot; preserve claims")
        if transcript_snapshot(record, path) != before:
            raise ValueError("Source transcript changed during termination checks")
        last = max(last, max(times)) if last else max(times)
        latest = max(latest, modified) if latest else modified
    return _TranscriptActivity(last, latest)


def _verify_artifacts(artifacts):
    for artifact in artifacts:
        if artifact.get("worktree") is None:
            continue  # Branch-only artifacts are checked against release inventory.
        path = Path(artifact["worktree"]).resolve()
        # Require the worktree root, not an arbitrary directory within a clone.
        root = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        branch = subprocess.run(["git", "-C", str(path), "symbolic-ref", "--quiet", "--short", "HEAD"],
                                capture_output=True, text=True, timeout=10, check=False)
        if branch.returncode == 1:
            # Bind an interrupted rebase/bisect to its recorded branch. A plain
            # detached checkout must descend from that branch's recorded ref.
            linked = False
            for name in ("rebase-merge/head-name", "rebase-apply/head-name", "BISECT_START"):
                state = subprocess.run(["git", "-C", str(path), "rev-parse", "--git-path", name],
                                       capture_output=True, text=True, timeout=10, check=True).stdout.strip()
                state_path = Path(state) if Path(state).is_absolute() else path / state
                if state_path.is_file() and state_path.read_text().strip() in {artifact["branch"], "refs/heads/" + artifact["branch"]}:
                    linked = True
            if not linked:
                subprocess.run(["git", "-C", str(path), "merge-base", "--is-ancestor", "refs/heads/" + artifact["branch"], "HEAD"],
                               capture_output=True, text=True, timeout=10, check=True)
        elif branch.returncode != 0 or branch.stdout.strip() != artifact["branch"]:
            raise ValueError("Retained worktree does not match its recorded branch")
        if Path(root).resolve() != path:
            raise ValueError("Retained worktree does not match its recorded root and branch")


def body(data):
    validate(data, data.get("claim_session", data["session_id"]))
    legacy = data.get("claim", {})
    legacy_fields = (f"Worker: {legacy['worker']}\nSource branch: {legacy['branch']}\n"
                     f"Native session unavailable: {legacy['legacy_reason']}\n") if "legacy_reason" in legacy else ""
    artifacts = "\n".join(f"- {a.get('worktree') or 'no local worktree'} (branch {a['branch']})" for a in data["retained_artifacts"]) or "None; no source branch or worktree was created."
    return (f"Terminated session {data.get('claim_session', data['session_id'])}\nEnded at: {data['ended_at']}\n"
            + legacy_fields + f"Recorded PID: {data['pid'] or 'not recorded'}\nTermination cause: {data['cause']}\n"
            f"Fresh process, transcript and terminal checks: {data['checked_at']}; {data['terminal_state']}.\n"
            f"Retained artifacts (resume; preserve uncommitted work):\n\n{artifacts}\n\n"
            "<!-- " + MARKER + json.dumps(data, sort_keys=True).replace(">", "\\u003e") + " -->")


def verify_attestation(record_path, text, session, *, source=None, source_url=None):
    """Recheck now without changing the historical termination time or evidence."""
    attested = evidence(text, session)
    if attested is None:
        if record_path:
            raise ValueError("--termination-record requires a terminated-session attestation")
        return
    if not record_path:
        raise ValueError("Terminated-session release requires --termination-record for fresh source-host checks")
    record = json.loads(Path(record_path).read_text(encoding="utf-8"))
    if record.get("exit_record"):
        import github_plan_release
        if (source is None or record.get("claim") != github_plan_release.record_from_receipt(source, record["claim"])
                or record.get("claim_url") != source_url or record.get("claim_created_at") != source["created_at"]):
            raise ValueError("Termination alias no longer matches the exact original claim")
    observed = verify(record, session)
    keys = ((set(observed) | set(attested)) - {"checked_at"} if record.get("exit_record") else
            {"session_id", "pid", "host", "ended_at", "cause", "retained_artifacts", "tmux_session_id", "booted_at"})
    for key in keys:
        if observed.get(key) != attested.get(key):
            raise ValueError("Termination record no longer matches the attested source identity or retained artifacts")
    # The attestation can be reused after a retry, but fresh probes always run.


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True, help="Private original-session record, augmented with crash facts and retained artifacts")
    args = parser.parse_args()
    try:
        original = json.loads(args.record.read_text(encoding="utf-8"))
        print(body(verify(original, original.get("claim_session", original["session_id"]))))
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")
