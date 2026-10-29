#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
import argparse
import asyncio
import json
import time
from pathlib import Path
from subprocess import SubprocessError

import iterm_tab
import pending_reviews
import session_liveness
from check_changes import fingerprint, save_state
from check_operation import (
    GateFailed,
    GitHubReader,
    GitHubReadError,
    GitHubReadShapeError,
    Operation,
    receipt,
    automation_only_gh_prefix_args,
    claim_release,
    closure,
    github_identity,
    native,
    observation_homes,
    release,
)
from session_record import load_ledger, normalize_ledger_entry, session_status

SCRIPTS = Path(__file__).resolve().parent


def require(condition: bool, gate: str, reason: str) -> None:
    if not condition:
        raise GateFailed(gate, reason)


def explicit_session(body: str, session: str) -> bool:
    return claim_release.related_sessions_match(body, session) and not claim_release.related_sessions_match(body, "other-" + session)


async def native_idle(entry: dict) -> None:
    if entry["harness"] != "codex":
        return

    home = Path(entry["codex_home"]).expanduser().resolve(strict=True)
    action = closure.lease_state(entry["session_id"])
    homes = {home, *(Path(value).resolve() for value in closure.action_homes(action)["known"])}
    snapshots = closure.transcripts(entry["session_id"], entry["transcript"], include_status=True, additional_homes=homes)
    require(not any(row["status"].get("background_tasks") or row["status"].get("active_realtime_sessions")
                    or row["status"].get("realtime_coverage") is False for row in snapshots.values()),
            "native_idle", "an authenticated source copy has background work or incomplete lifecycle coverage")
    paths = native.authenticated_paths(snapshots, observation_homes(strict=True, additional_homes=homes))
    homes.update(Path(value) for value in paths)
    snapshot = {"path": entry["transcript"], "transcripts": snapshots, "home_paths": paths}
    for current in homes:
        async with native.connection(current) as request:
            facts = await native.read_thread_facts(request, entry["session_id"])
        native.require_quiet(facts, native.local_snapshot(snapshot, facts, str(current)), native.home_action(action, str(current)))


def ready(entry: dict, supervisor: str) -> dict:
    require(
        entry.get("supervisor_owned") is True and entry["session_id"] != supervisor,
        "ownership",
        "requires another explicitly Supervisor-owned session",
    )
    try:
        status = session_status(entry)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise GateFailed("verdict", str(error)) from error
    require(
        status["safe_verdict"] and status["state"] == "turn_ended" and not status["background_tasks"],
        "verdict",
        "requires a completed unqualified verdict without background tasks",
    )
    return status


def closeout(args: argparse.Namespace, operation: Operation) -> None:
    entries = [entry for entry in load_ledger(args.ledger) if entry["session_id"] == args.session_id]
    retired = not entries and bool(operation.state.get("retired_generation"))
    if retired:
        with closure.ownership(args.session_id, create=True):
            saved = closure.source_records({args.session_id}).get(args.session_id)
            require(bool(saved), "identity", "retired source has no canonical closure")
            release.validate_closure(saved, args.supervisor_session)
            entries = [saved]
    require(len(entries) == 1, "identity", "native session must match exactly one ledger entry or verified retirement")
    entry = entries[0]
    selected_handoff = getattr(args, "handoff_url", None)
    if selected_handoff:
        require(not entry.get("handoff_url") or entry["handoff_url"] == selected_handoff, "handoff", "recorded handoff differs; preserve it")
        entry["handoff_url"] = selected_handoff
    generation = fingerprint({key: value for key, value in release.closure_identity(entry).items() if key != "handoff_url"})
    require(operation.state.get("generation", generation) == generation, "identity", "journal belongs to another launch generation")
    require(not retired or operation.state["retired_generation"] == generation, "identity", "retired source generation changed")
    operation.state["generation"] = generation
    save_state(operation.path, operation.state)
    ready(entry, args.supervisor_session)
    backend = entry.get("backend", "iterm")
    selected_terminal = entry.get("tmux_session_id" if backend == "tmux" else "iterm_session_id")
    if not isinstance(selected_terminal, str) or not selected_terminal:
        raise GateFailed("identity", "thread-only session has no matched terminal")
    terminal_id = selected_terminal
    terminal = ["uv", "run", str(SCRIPTS / "iterm_tab.py"), "--backend", backend]

    def call(gate: str, action: str, *extra: str, once: bool = False) -> dict:
        return operation.run(gate, [*terminal, action, "--session-id", terminal_id, *extra], once=once)

    def verify_handoff() -> None:
        operation.gate = "handoff"
        reader = GitHubReader(gh_prefix_args=automation_only_gh_prefix_args(), deadline_at=time.time() + 60)
        handoff = release.handoff(reader, entry, github_identity.automation_login())
        require(
            explicit_session(handoff.get("body") or "", args.session_id),
            "handoff",
            "handoff must name this native session",
        )
        if selected_handoff and not retired:
            with closure.store_lock(args.ledger, create=True):
                rows = json.loads(args.ledger.read_text())
                matches = [row for row in rows if row.get("session_id") == args.session_id]
                require(
                    len(matches) == 1
                    and release.closure_identity(normalize_ledger_entry(args.ledger, {**matches[0], "handoff_url": selected_handoff})) == release.closure_identity(entry),
                    "identity",
                    "ledger generation changed before handoff update",
                )
                matches[0]["handoff_url"] = selected_handoff
                save_state(args.ledger, rows)

    def claims() -> None:
        result = operation.run(
            "claims",
            [
                "uv", "run",
                str(SCRIPTS.parents[1] / "work-closeout/scripts/check_claims.py"),
                "--owner", args.owner,
                "--session", args.session_id,
                "--transcript", entry["transcript"],
            ],
            timeout=660,
        )
        require(
            result.get("claims_clear") is True and result.get("coverage", {}).get("complete") is True,
            "claims",
            "claim releases or discovery coverage are incomplete",
        )
        operation.gate = "review_wait"
        reader = GitHubReader(gh_prefix_args=automation_only_gh_prefix_args(), deadline_at=time.time() + 120)
        actor = github_identity.automation_login()
        if not actor:
            raise GateFailed("identity", "configured automation identity is unknown")
        logins = {actor.casefold()}
        for url in {url for row in result.get("released_claims", []) for url in row.get("retained_prs", [])}:
            repo, number = url.removeprefix("https://github.com/").rsplit("/pull/", 1)
            comments = reader.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="closeout_review")
            request = pending_reviews.latest_request(comments, logins)
            if request:
                pull = reader.get_json(f"/repos/{repo}/pulls/{number}", step="closeout_review_head")
                completion = pending_reviews.completion_record(comments, request, pull["head"]["sha"], logins)
                require(
                    bool(completion and completion.get("acknowledged")),
                    "review_wait",
                    "required review still needs its current-head disposition",
                )

    def prompt(gate: str) -> None:
        pane = call(gate, "read")
        preservation = operation.evidence / (gate + "-" + str(time.time_ns()) + ".json")
        save_state(preservation, {"identity": release.closure_identity(entry), "readback": pane})
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
            and session_liveness.source_harness(session_liveness.harness_arguments(row[2]), entry["harness"])
        ]
        require(len(matches) == 1, "process", "matched harness PID and TTY are unavailable")
        if entry.get("launch_started_at"):
            require(
                session_liveness.process_started_at(entry["pid"]).isoformat() == entry["launch_started_at"],
                "process",
                "process birth differs from this launch generation",
            )
        require(not pane.get("dead"), "prompt", "terminal process already ended; use verified recovery")
        screen = "\n".join(pane["screen"]) if isinstance(pane["screen"], list) else pane["screen"]
        require("esc to interrupt" not in screen.casefold() and "Working (" not in screen, "prompt", "turn appears to be running")
        require(
            iterm_tab.empty_agent_input(pane),
            "input",
            "nonempty or uncertain input needs preservation and manual verification",
        )
        ready(entry, args.supervisor_session)

    def wait_for_exit() -> None:
        operation.gate = "process_exit"
        deadline = time.monotonic() + args.exit_seconds
        while not session_liveness.process_absent(entry.get("pid")) and time.monotonic() < deadline:
            time.sleep(0.25)
        require(session_liveness.process_absent(entry.get("pid")), "process_exit", "process remains; terminal kept")

    with closure.ownership(args.session_id, create=True):
        claims()
        verify_handoff()
        require(not retired or session_liveness.process_absent(entry.get("pid")), "process_exit", "retired process identity is live")
        if not session_liveness.process_absent(entry.get("pid")) and not operation.state["steps"].get("quit"):
            operation.gate = "native_idle"
            asyncio.run(native_idle(entry))
            prompt("prompt")
            call("clear", "clear", "--verified-target")
            prompt("empty_input")
            operation.gate = "native_idle"
            asyncio.run(native_idle(entry))
            prompt("final")
            call("quit", "send", "--text", "/quit" if entry["harness"] == "codex" else "/exit", "--verified-target", once=True)
            wait_for_exit()
        elif not session_liveness.process_absent(entry.get("pid")):
            require(
                operation.state["steps"]["quit"].get("complete") is True,
                "quit",
                "previous send is uncertain; preserve the session for reconciliation",
            )
            wait_for_exit()
    require(session_liveness.process_absent(entry.get("pid")), "process_exit", "process remains; terminal kept")
    ready(entry, args.supervisor_session)
    if not retired:
        finish = operation.run(
            "finish",
            [
                "uv", "run",
                str(SCRIPTS / "release_session_claims.py"),
                "finish",
                "--ledger", str(args.ledger),
                "--session-id", args.session_id,
                "--supervisor-session", args.supervisor_session,
                "--owner", args.owner,
                "--confirm-session-ended", "--apply",
            ],
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
    operation.gate = "retirement"
    with closure.ownership(args.session_id, create=True):
        saved = closure.source_records({args.session_id})[args.session_id]
        require(not saved.get("resumed_at") and release.closure_identity(saved) == release.closure_identity(entry),
                "retirement", "canonical closure does not preserve this finished generation")
        release.validate_closure(saved, args.supervisor_session)
        with closure.store_lock(args.ledger, create=True):
            active_rows = iterm_tab.read_launch_ledger(args.ledger)
            active_matches = [normalize_ledger_entry(args.ledger, row) for row in active_rows if row.get("session_id") == args.session_id]
            require(active_matches == ([] if retired else [entry]), "retirement", "active source changed; preserve the ledger")
            operation.state["retired_generation"] = generation
            save_state(operation.path, operation.state)
            if not retired:
                save_state(args.ledger, [row for row in active_rows if row.get("session_id") != args.session_id])


def main() -> int:
    parser = argparse.ArgumentParser(description="Close one normal finished session, stopping at the first failed gate.")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--supervisor-session", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--handoff-url", help="exact posted issue handoff; verify and save it in the ledger before shutdown")
    parser.add_argument("--exit-seconds", type=float, default=30)
    args = parser.parse_args()
    try:
        require(0 < args.exit_seconds <= 60, "arguments", "exit wait must be between zero and 60 seconds")
        with closure.store_lock(args.ledger.with_name(args.ledger.name + ".closeout-" + args.session_id), create=True):
            operation = Operation(args.evidence, [str(args.ledger.resolve()), args.session_id])
            closeout(args, operation)
        result = receipt(args.session_id, args.evidence)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, SubprocessError, GitHubReadError, GitHubReadShapeError) as error:
        result = receipt(args.session_id, args.evidence, error,
                         gate=operation.gate if "operation" in locals() else "identity")
    print(json.dumps(result, separators=(",", ":")))
    return int(not result["ok"])


if __name__ == "__main__":
    raise SystemExit(main())
