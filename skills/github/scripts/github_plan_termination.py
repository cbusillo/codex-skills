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

# Reuse the native transcript reader; verdicts are deliberately not required.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "supervisor/scripts"))
import session_record
from close_ttys import inventory, idle_terminal_command

MARKER = "github-plan:termination "
QUIET_PERIOD = timedelta(minutes=5)


def stamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Termination evidence requires timezone-qualified timestamps")
    return result


def validate(data, session):
    if not isinstance(data, dict) or data.get("session_id") != session:
        raise ValueError("Termination evidence must identify the exact native session")
    if type(data.get("pid")) is not int or data["pid"] <= 0:
        raise ValueError("Termination evidence requires the recorded source PID")
    if not isinstance(data.get("host"), str) or not data["host"]:
        raise ValueError("Termination evidence requires the recorded source host")
    if not isinstance(data.get("cause"), str) or not data["cause"].strip():
        raise ValueError("Termination evidence requires the cause of termination")
    artifacts = data.get("retained_artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("Termination evidence must name retained worktrees and branches")
    for artifact in artifacts:
        if (not isinstance(artifact, dict)
                or not isinstance(artifact.get("branch"), str) or not artifact["branch"].strip()
                or not isinstance(artifact.get("worktree"), str) or not Path(artifact["worktree"]).is_absolute()):
            raise ValueError("Each retained artifact requires a branch and absolute worktree path")
    ended, checked, last = (stamp(data[key]) for key in ("ended_at", "checked_at", "last_activity_at"))
    if last > ended or ended > checked or checked - last < QUIET_PERIOD:
        raise ValueError("Source transcript has recent activity or activity after termination")
    if data.get("terminal_state") not in {"pane dead", "server missing"}:
        raise ValueError("Termination evidence requires a dead pane or missing server")
    return data


def evidence(text, session):
    lines = text.splitlines()
    if lines[:1] != [f"Terminated session {session}"]:
        return None
    records = [line for line in lines if line.startswith("<!-- " + MARKER) and line.endswith(" -->")]
    if len(records) != 1:
        raise ValueError("Terminated-session attestation requires one termination evidence record")
    data = json.loads(records[0][len("<!-- " + MARKER):-len(" -->")])
    return validate(data, session)


def terminal_state(record):
    server, pane = record.get("tmux_socket"), record.get("tmux_pane")
    if (not isinstance(server, str) or not re.fullmatch(r"[\w.-]+", server)
            or not isinstance(pane, str) or not re.fullmatch(r"%\d+", pane)):
        raise ValueError("Termination record requires its original tmux socket name and pane ID")
    result = run_terminal(["tmux", "-L", server, "list-panes", "-a", "-F", "#{pane_id}\t#{pane_dead}"],
                            capture_output=True, text=True, timeout=10, check=False)
    if result.returncode:
        error = result.stderr.strip()
        if result.returncode == 1 and (
            error.startswith("no server running on ")
            or error.startswith("error connecting to ") and error.endswith("(No such file or directory)")
        ):
            return "server missing"
        raise ValueError("Cannot verify the original tmux server; preserve claims")
    if f"{pane}\t1" in result.stdout.splitlines():
        return "pane dead"
    raise ValueError("Original tmux pane is live or its identity is uncertain")


def verify(record, session):
    """Never infer termination from absence alone; caller supplies original identity."""
    if record.get("session_id") != session or record.get("host") != socket.gethostname():
        raise ValueError("Run termination checks on the recorded source host with the exact native session")
    pid, tty = record.get("pid"), record.get("tty")
    if type(pid) is not int or pid <= 0 or not isinstance(tty, str) or not tty.strip():
        raise ValueError("Termination record requires the recorded source PID and TTY")
    tty = tty.removeprefix("/dev/")
    rows = inventory()
    for process, process_tty, command in rows:
        if process == pid or process_tty == tty and not idle_terminal_command(command):
            raise ValueError("Source process or work on its TTY is still running")
        argv = command.split()
        resumed = [argv[i + 1].strip("\"'") for i, arg in enumerate(argv[:-1]) if arg in {"resume", "--resume", "-r"}]
        resumed.extend(arg.split("=", 1)[1].strip("\"'") for arg in argv if arg.startswith("--resume="))
        if session in resumed:
            raise ValueError("Source native session has a running resumed process")
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
        entry = {**record, "transcript": str(path)}
        before = session_record.session_status(entry)
        records = session_record.read_jsonl(path)
        times = [stamp(row["timestamp"]) for row in records if row.get("timestamp")]
        if not times or max(times) > ended:
            raise ValueError("Source transcript is uncertain or resumed after termination")
        activity = max(*times, datetime.fromtimestamp(before["mtime"], timezone.utc))
        if now - activity < QUIET_PERIOD:
            raise ValueError("Source transcript activity is recent; preserve claims")
        if session_record.session_status(entry) != before:
            raise ValueError("Source transcript changed during termination checks")
        last = max(last, max(times)) if last else max(times)
    data = {key: record[key] for key in ("session_id", "pid", "host", "ended_at", "cause", "retained_artifacts")}
    data.update(checked_at=now.isoformat(), last_activity_at=last.isoformat(), terminal_state=terminal_state(record))
    return validate(data, session)


def body(data):
    validate(data, data["session_id"])
    artifacts = "\n".join(f"- {a['worktree']} (branch {a['branch']})" for a in data["retained_artifacts"])
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
    for key in ("session_id", "pid", "host", "ended_at", "cause", "retained_artifacts"):
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
