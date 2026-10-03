#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["iterm2==2.25"]
# ///
"""Close an exact iTerm session only after its verified agent process has exited.

Exit commands and pending permissions are handled by the Supervisor's procedure,
not by this helper. Default is a dry run; it never force-closes a session.
"""

import argparse
import json
import subprocess
from pathlib import Path

import session_record
from iterm_tab import select_session


def process_rows(output):
    rows = []
    for line in output.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3 or not fields[0].isdigit():
            raise ValueError("unparseable process inventory")
        rows.append((int(fields[0]), fields[1], fields[2]))
    return rows


def process_exited(entry, rows):
    pid, tty = entry.get("pid"), entry.get("tty")
    if not isinstance(pid, int) or pid <= 0 or not isinstance(tty, str) or not tty:
        raise ValueError("ledger requires verified pid and tty")
    # A reused PID or any non-shell job on this TTY preserves the tab.
    if any(row[0] == pid for row in rows):
        return False
    return not any(
        row[1] == tty and Path(row[2]).name not in {"zsh", "bash", "sh", "fish", "dash"}
        for row in rows
    )


def inventory():
    output = subprocess.run(
        ["ps", "-axo", "pid=,tty=,comm="],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    return process_rows(output)


async def close(app, entry, apply, verified_handoff, verified_input):
    if entry.get("supervisor_owned") is not True:
        raise ValueError("session is not explicitly Supervisor-owned")
    if not verified_handoff or not verified_input or not entry.get("handoff_url"):
        raise ValueError(
            "verify issue handoff and recorded/empty Director input before closing"
        )
    status = session_record.session_status(entry)
    if not status["safe_verdict"]:
        raise ValueError(
            "latest transcript does not contain an unqualified close-out verdict"
        )
    session = select_session(app, entry.get("iterm_session_id"))
    tty = (await session.async_get_variable("tty") or "").removeprefix("/dev/")
    if tty != entry.get("tty"):
        raise ValueError("terminal identity differs from ledger")
    if not process_exited(entry, inventory()):
        raise ValueError("agent process remains or identity is uncertain; tab kept")
    if apply:
        # Repeat both checks immediately before the one non-force close call.
        fresh = session_record.session_status(entry)
        if fresh != status or not process_exited(entry, inventory()):
            raise ValueError("session activity changed; tab kept")
        await session.async_close(force=False)
    return {"session_id": entry["session_id"], "closed": apply, "dry_run": not apply}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verified-handoff", action="store_true")
    parser.add_argument("--verified-input", action="store_true")
    args = parser.parse_args()
    matches = [
        entry
        for entry in session_record.load_ledger(args.ledger)
        if entry["session_id"] == args.session_id
    ]
    if len(matches) != 1:
        parser.error("session id must match one ledger entry")
    import iterm2

    async def connected(connection):
        app = await iterm2.async_get_app(connection)
        try:
            result = await close(
                app, matches[0], args.apply, args.verified_handoff, args.verified_input
            )
            print(json.dumps(result))
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            parser.exit(1, f"refused: {error}\n")

    iterm2.run_until_complete(connected)


if __name__ == "__main__":
    main()
