# /// script
# requires-python = ">=3.12"
# dependencies = []
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


def validate(data, session, *, apply_policy=True):
    if not isinstance(data, dict) or data.get("session_id") != session:
        raise ValueError("Termination evidence must identify the exact native session")
    if type(data.get("pid")) is not int or data["pid"] <= 0:
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
    ended, checked, last = (stamp(data[key]) for key in ("ended_at", "checked_at", "last_activity_at"))
    if last > ended or ended > checked or apply_policy and checked - last < QUIET_PERIOD:
        raise ValueError("Source transcript has recent activity or activity after termination")
    if (not isinstance(data.get("terminal_state"), str) or not data["terminal_state"]
            or apply_policy and data["terminal_state"] not in {"pane dead", "server missing", "server replaced"}):
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
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    records, truncated = transcript_records(record, lines)
    after = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise ValueError("Source transcript changed during termination checks")
    return records, after.st_mtime_ns, after.st_size, truncated


def transcript_records(record, lines):
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
    if identities != {record["session_id"]}:
        raise ValueError("Source transcript identity is uncertain")
    return records, truncated


def _transcript_activity(times, mtime_ns, truncated, now, ended):
    if not times or max(times) > ended:
        raise ValueError("Source transcript is uncertain or resumed after termination")
    modified = datetime.fromtimestamp(mtime_ns / 1e9, timezone.utc)
    if truncated and modified > ended:
        raise ValueError("Truncated transcript has activity after termination")
    if now - max(*times, modified) < QUIET_PERIOD:
        raise ValueError("Source transcript activity is recent; preserve claims")
    return max(times)


def verify(record, session):
    """Never infer termination from absence alone; caller supplies original identity."""
    import github_plan_closure
    return _verify(record, session, lambda pid, argv: not github_plan_closure.action_pending(session)
                   and github_plan_closure.guarded_launch_endpoint(pid, argv) is not None)[0]


class _TerminationProcess(NamedTuple):
    original: dict
    physical: dict
    cutoff: datetime | None


def _verify_queue_source(record, session, process=None, *, latest_pid=None):
    """Physical termination for cleanup; return context, never a release attestation."""
    import github_plan_closure
    return _verify(record, session, lambda pid, argv: github_plan_closure.guarded_launch_endpoint(pid, argv) is not None,
                   process=process, latest_pid=latest_pid)[1]


def _physical_exit(process, independent_harness):
    native_helpers()[1].verify_process_exit(process.physical, rows=inventory(), ended_at=process.cutoff,
                                           independent_harness=independent_harness)


def _recheck_queue_process(process):
    import github_plan_closure
    _physical_exit(process, lambda pid, argv: github_plan_closure.guarded_launch_endpoint(pid, argv) is not None)


def _verify(record, session, independent_harness, *, process=None, latest_pid=None):
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
    if process is None:
        physical = dict(record)
        if latest_pid is not None and latest_pid not in {record.get("pid"), record.get("launch_pid")}:
            if record.get("launch_pid") is not None:
                raise ValueError("multiple known source launch PIDs require matching original context")
            physical["launch_pid"] = latest_pid  # Negative veto only; never supplies native pane authority.
        process = _TerminationProcess(dict(record), physical, shared.native_exit_cutoff(record))
    elif process.original != record:
        raise ValueError("termination source identity changed")
    cutoff = process.cutoff
    if cutoff is not None and cutoff > stamp(record["ended_at"]):
        raise ValueError("Native pane exit follows the proposed termination time")
    _physical_exit(process, independent_harness)
    roots = record.get("transcript_roots")
    if not isinstance(roots, list) or not roots or not all(isinstance(root, str) and Path(root).is_dir() for root in roots):
        raise ValueError("Supply every known source-account transcript root on the source host")
    paths = {Path(record["transcript"]).resolve()}
    for root in roots:
        paths.update(path.resolve() for path in Path(root).rglob(f"*{session}*.jsonl"))
    now = datetime.now(timezone.utc)
    ended = stamp(record["ended_at"])
    last = None
    for path in paths:
        before = transcript_snapshot(record, path)
        records, mtime_ns, _size, truncated = before
        times = [stamp(row["timestamp"]) for row in records if row.get("timestamp")]
        _transcript_activity(times, mtime_ns, truncated, now, ended)
        if transcript_snapshot(record, path) != before:
            raise ValueError("Source transcript changed during termination checks")
        last = max(last, max(times)) if last else max(times)
    data = {key: record[key] for key in ("session_id", "pid", "host", "ended_at", "cause", "retained_artifacts", "tmux_session_id")}
    data.update(checked_at=now.isoformat(), last_activity_at=last.isoformat(), terminal_state=terminal_state(record))
    validate(data, session)
    for artifact in data["retained_artifacts"]:
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
    return data, process


def body(data):
    validate(data, data["session_id"])
    artifacts = "\n".join(f"- {a.get('worktree') or 'no local worktree'} (branch {a['branch']})" for a in data["retained_artifacts"]) or "None; no source branch or worktree was created."
    return (f"Terminated session {data['session_id']}\nEnded at: {data['ended_at']}\n"
            f"Recorded PID: {data['pid']}\nTermination cause: {data['cause']}\n"
            f"Fresh process, transcript and terminal checks: {data['checked_at']}; {data['terminal_state']}.\n"
            f"Retained artifacts (resume; preserve uncommitted work):\n\n{artifacts}\n\n"
            "<!-- " + MARKER + json.dumps(data, sort_keys=True).replace(">", "\\u003e") + " -->")


def verify_attestation(record_path, text, session):
    """Recheck now without changing the historical termination time or evidence."""
    attested = evidence(text, session)
    if attested is None:
        if record_path:
            raise ValueError("--termination-record requires a terminated-session attestation")
        return
    if not record_path:
        raise ValueError("Terminated-session release requires --termination-record for fresh source-host checks")
    record = json.loads(Path(record_path).read_text(encoding="utf-8"))
    observed = verify(record, session)
    for key in ("session_id", "pid", "host", "ended_at", "cause", "retained_artifacts", "tmux_session_id"):
        if observed[key] != attested[key]:
            raise ValueError("Termination record no longer matches the attested source identity or retained artifacts")
    # The attestation can be reused after a retry, but fresh probes always run.


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True, help="Private original-session record, augmented with crash facts and retained artifacts")
    args = parser.parse_args()
    try:
        original = json.loads(args.record.read_text(encoding="utf-8"))
        print(body(verify(original, original["session_id"])))
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")
