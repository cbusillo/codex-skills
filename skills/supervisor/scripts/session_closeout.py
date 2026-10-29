#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import argparse
import json
import subprocess
import time
from pathlib import Path

import release_session_claims as release
import session_liveness
from check_operation import GateFailed, Operation
from session_record import load_ledger, session_status

SCRIPTS = Path(__file__).resolve().parent


def require(condition: bool, gate: str, reason: str) -> None:
    if not condition:
        raise GateFailed(gate, reason)


def ready(entry: dict, supervisor: str) -> dict:
    require(
        entry.get("supervisor_owned") is True and entry["session_id"] != supervisor,
        "ownership",
        "requires another explicitly Supervisor-owned session",
    )
    status = session_status(entry)
    require(
        status["safe_verdict"] and status["state"] == "turn_ended" and not status["background_tasks"],
        "verdict",
        "requires a completed unqualified verdict without background tasks",
    )
    return status


def closeout(args: argparse.Namespace, operation: Operation) -> None:
    entries = [entry for entry in load_ledger(args.ledger) if entry["session_id"] == args.session_id]
    require(len(entries) == 1, "identity", "native session must match exactly one ledger entry")
    entry = entries[0]
    ready(entry, args.supervisor_session)
    backend = entry.get("backend", "iterm")
    terminal_id = entry.get("tmux_session_id" if backend == "tmux" else "iterm_session_id")
    require(bool(terminal_id), "identity", "thread-only session has no matched terminal")
    terminal = ["uv", "run", str(SCRIPTS / "iterm_tab.py"), "--backend", backend]

    def call(gate: str, action: str, *extra: str, once: bool = False) -> dict | list:
        return operation.run(gate, [*terminal, action, "--session-id", terminal_id, *extra], once=once)

    def verify_handoff() -> None:
        reader = release.GitHubReader(gh_prefix_args=release.automation_only_gh_prefix_args(), deadline_at=time.time() + 60)
        handoff = release.handoff(reader, entry, release.github_identity.automation_login())
        require(
            release.release.related_sessions_match(handoff.get("body") or "", args.session_id),
            "handoff",
            "handoff must name this native session",
        )

    def claims() -> None:
        result = operation.run(
            "claims",
            [
                "uv",
                "run",
                str(SCRIPTS.parents[1] / "work-closeout/scripts/check_claims.py"),
                "--owner",
                args.owner,
                "--session",
                args.session_id,
                "--transcript",
                entry["transcript"],
            ],
        )
        require(
            result.get("claims_clear") is True and result.get("coverage", {}).get("complete") is True,
            "claims",
            "claim releases or discovery coverage are incomplete",
        )

    def prompt(gate: str) -> None:
        pane = call(gate, "read")
        terminals = operation.run(gate + "_identity", [*terminal, "list"])
        matches = [row for row in terminals if row["session_id"] == terminal_id]
        require(len(matches) == 1, "identity", "terminal identity is not unique")
        identity = matches[0]
        require(identity.get("tty", "").removeprefix("/dev/") == entry.get("tty"), "identity", "terminal TTY differs")
        for key in ("window_id", "tab_id"):
            require(key not in entry or identity.get(key) == entry[key], "identity", f"terminal {key} differs")
        rows = session_liveness.inventory()
        matches = [
            row
            for row in rows
            if row[0] == entry.get("pid")
            and row[1] == entry.get("tty")
            and session_liveness.harness_family((session_liveness.harness_arguments(row[2]) or [""])[0]) == entry["harness"]
        ]
        require(len(matches) == 1, "process", "matched harness PID and TTY are unavailable")
        require(not pane.get("dead"), "prompt", "terminal process already ended; use verified recovery")
        screen = "\n".join(pane["screen"]) if isinstance(pane["screen"], list) else pane["screen"]
        require("esc to interrupt" not in screen.casefold() and "Working (" not in screen, "prompt", "turn appears to be running")
        line = pane.get("input_line") or {}
        require(
            line.get("style") == "empty"
            or entry["harness"] == "codex"
            and line.get("style") == "dim"
            and line.get("text") == "Ask Codex to do anything",
            "input",
            "nonempty or uncertain input needs preservation and manual verification",
        )

    verify_handoff()
    claims()
    if not operation.state["steps"].get("quit") and not session_liveness.process_absent(entry.get("pid")):
        prompt("prompt")
        ready(entry, args.supervisor_session)
        call("clear", "clear", "--verified-target", once=True)
        prompt("empty_input")
        ready(entry, args.supervisor_session)
        verify_handoff()
        claims()
        call("quit", "send", "--text", "/quit" if entry["harness"] == "codex" else "/exit", "--verified-target", once=True)
    elif operation.state["steps"].get("quit"):
        require(
            operation.state["steps"]["quit"].get("complete") is True,
            "quit",
            "previous send is uncertain; preserve the session for reconciliation",
        )
    deadline = time.monotonic() + args.exit_seconds
    while not session_liveness.process_absent(entry.get("pid")) and time.monotonic() < deadline:
        time.sleep(0.25)
    require(session_liveness.process_absent(entry.get("pid")), "process_exit", "process remains; terminal kept")
    ready(entry, args.supervisor_session)
    finish = operation.run(
        "finish",
        [
            "uv",
            "run",
            str(SCRIPTS / "release_session_claims.py"),
            "finish",
            "--ledger",
            str(args.ledger),
            "--session-id",
            args.session_id,
            "--supervisor-session",
            args.supervisor_session,
            "--owner",
            args.owner,
            "--confirm-session-ended",
            "--apply",
        ],
        once=True,
        timeout=660,
    )
    require(
        finish.get("ok") is True and finish.get("needs_reconciliation") is False,
        "finish",
        "claim discovery or release needs reconciliation; terminal kept",
    )
    panes = operation.run("terminals", [*terminal, "list"])
    if any(pane["session_id"] == terminal_id for pane in panes):
        call("close", "close", "--ledger", str(args.ledger), once=True)
    panes = operation.run("disappearance", [*terminal, "list"])
    require(not any(pane["session_id"] == terminal_id for pane in panes), "disappearance", "terminal closure is uncertain")


def main() -> int:
    parser = argparse.ArgumentParser(description="Close one normal finished session, stopping at the first failed gate.")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--supervisor-session", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--exit-seconds", type=float, default=30)
    args = parser.parse_args()
    try:
        require(0 < args.exit_seconds <= 60, "arguments", "exit wait must be between zero and 60 seconds")
        with release.closure.store_lock(args.ledger.with_name(args.ledger.name + ".closeout-" + args.session_id), create=True):
            operation = Operation(args.evidence, [str(args.ledger.resolve()), args.session_id, args.supervisor_session])
            closeout(args, operation)
        result = operation.receipt(args.session_id)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        result = {
            "ok": False,
            "identity": args.session_id,
            "gate": error.gate if isinstance(error, GateFailed) else "verification",
            "reason": str(error),
            "evidence": str(args.evidence),
        }
    print(json.dumps(result, separators=(",", ":")))
    return int(not result["ok"])


if __name__ == "__main__":
    raise SystemExit(main())
