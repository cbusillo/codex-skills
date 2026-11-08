#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from subprocess import SubprocessError
from typing import TYPE_CHECKING

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
    automation_only_gh_prefix_args,
    claim_release,
    closure,
    github_identity,
    native,
    observation_homes,
    receipt,
    release,
)
from session_record import load_ledger, normalize_ledger_entry, session_status

SCRIPTS = Path(__file__).resolve().parent

if TYPE_CHECKING:
    from skills.github.scripts.github_catalog_health import MARKER as OPERATION_MARKER
    from skills.github.scripts.github_plan_claim import (
        live_lines,
        session_fields,
        unquoted_text,
    )
else:
    from github_catalog_health import MARKER as OPERATION_MARKER
    from github_plan_claim import live_lines, session_fields, unquoted_text


def require(condition: bool, gate: str, reason: str) -> None:
    if not condition:
        raise GateFailed(gate, reason)


def explicit_session(body: str, session: str) -> bool:
    text = re.sub(r"<!--.*?-->", "", unquoted_text(body), flags=re.DOTALL)
    if session_fields(text) and not session_fields(text, current_only=True):
        return False
    return (claim_release.related_sessions_match(text, session)
            and re.search(rf"(?<![\w-]){re.escape(session)}(?![\w-])", text) is not None)


async def native_idle(entry: dict) -> None:
    if entry["harness"] == "agy":
        if session_liveness.process_absent(entry.get("pid")):
            return
        try:
            log = Path(entry["agy_log"]).read_text()
        except (KeyError, TypeError, OSError, UnicodeError) as error:
            raise GateFailed("identity", "live AGY conversation identity is unavailable; preserve its terminal and receipt") from error
        resumed = re.findall(r"\bResuming conversation[^\S\n]*([^\n]*)", log)
        require(iterm_tab.claim_history.agy_conversations(log) == [entry["session_id"]]
                and all(re.match(re.escape(entry["session_id"]) + r"(?:$|\s|\()", value) for value in resumed),
                "identity", "AGY process may host another conversation; preserve its terminal")
        return
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


def claim_evidence(entry: dict, operation: Operation, key: str) -> list[dict]:
    path = operation.evidence / "claims.json"
    if not path.exists():
        return []
    receipt_data = json.loads(path.read_text())
    checked = json.loads(receipt_data.get("stdout") or "{}")
    if (receipt_data.get("exit_code") != 0 or checked.get("ok") is not True
            or checked.get("claims_clear") is not True or checked.get("coverage", {}).get("complete") is not True
            or checked.get("session_ids") != [entry["session_id"]]
            or checked.get("blocked") != [] or checked.get("unresolved_invocations") != []):
        return []
    rows = checked.get(key, [])
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and row.get("source_session") == entry["session_id"]]


def projected_handoff(comment: dict, evidence: list[dict], actor: str, session: str) -> bool:
    body = comment.get("body") or ""
    if (not isinstance(comment.get("created_at"), str) or comment.get("created_at") != comment.get("updated_at")
            or (comment.get("user") or {}).get("login") != actor or live_lines(body)
            or re.match(r"(?i)^Claimed by\b", body.strip()) or not claim_release.related_sessions_match(unquoted_text(body), session)):
        return False
    return any(row.get("expected_actor") == actor and isinstance(posted := row.get("comment"), dict)
               and posted.get("author") == actor and posted.get("id") == comment.get("id")
               and posted.get("url") == comment.get("html_url") and posted.get("created_at") == posted.get("updated_at")
               and str(comment.get("issue_url") or "").casefold()
               == f"https://api.github.com/repos/{row.get('repository')}/issues/{row.get('number')}".casefold()
               for row in evidence)


def handoff_refusal(comment: dict, evidence: list[dict], session: str, actor: str) -> str:
    body = comment.get("body") or ""
    url = comment.get("html_url") or comment.get("id")
    if (comment.get("user") or {}).get("login") != actor:
        return f"{url}: author differs from {actor}"
    if live_lines(body) or re.match(r"(?i)^Claimed by\b", body.strip()):
        return f"{url}: claim comment is not a handoff"
    marker = OPERATION_MARKER.search(body)
    proof = "no operation marker" if marker is None else "operation marker has no matching write proof from this session"
    if marker and any((row.get("operation_marker") or {}).get("operation_id") == marker[1] for row in evidence):
        proof = "operation marker found, but comment does not match the source write"
        if comment.get("created_at") != comment.get("updated_at"):
            proof += "; comment was edited after creation"
    text = unquoted_text(body)
    identity = f"native session id {session} absent"
    if explicit_session(body, session):
        identity = f"native session id {session} present"
    elif session_fields(text) and not session_fields(text, current_only=True):
        identity = f"native session id {session} not identified as current; only historical session fields found"
    elif not claim_release.related_sessions_match(text, session):
        identity = f"native session fields conflict with {session}"
    return f"{comment.get('html_url') or comment.get('id')}: {identity}; {proof}"


def verify_handoff(entry: dict, args: argparse.Namespace, operation: Operation, *, retired: bool) -> None:
    operation.gate = "handoff"
    reader = GitHubReader(gh_prefix_args=automation_only_gh_prefix_args(), deadline_at=time.time() + 60)
    actor = github_identity.automation_login()
    if not actor:
        raise GateFailed("handoff", "configured automation identity is unknown")
    started = claim_release.stamp(entry["launch_started_at"]) if entry.get("launch_started_at") else None
    try:
        saved = closure.source_records({entry["session_id"]}).get(entry["session_id"]) if not retired else None
        associated = release.handoff_association(entry, saved, reader=reader, actor=actor)
    except (ValueError, OSError, GitHubReadError) as error:
        raise GateFailed("handoff", str(error)) from error
    selected = getattr(args, "handoff_url", None) or operation.state.get("handoff", {}).get("url") or associated
    require(not associated or associated == selected, "handoff", "current handoff differs; preserve it")
    evidence = None
    if not selected:
        repo, number = entry.get("repository"), entry.get("issue")
        if (not isinstance(repo, str) or release.REPO.fullmatch(repo) is None
                or not isinstance(number, int) or isinstance(number, bool) or number <= 0):
            raise GateFailed("handoff", "discovery needs the ledger's exact repository and issue")
        comments = reader.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="closeout_handoff")
        evidence = claim_evidence(entry, operation, "handoff_comments")
        releases = {int(match[3]) for row in claim_evidence(entry, operation, "released_claims")
                    if (match := claim_release.COMMENT_URL.fullmatch(row.get("release_url") or ""))}
        scoped = [row for row in evidence if str(row.get("repository") or "").casefold() == repo.casefold()
                  and row.get("number") == number]
        candidates = [comment for comment in comments
                      if (comment.get("user") or {}).get("login") == actor
                      and (started is None or claim_release.stamp(comment["created_at"]) >= started)
                      and not live_lines(comment.get("body") or "")
                      and not re.match(r"(?i)^Claimed by\b", (comment.get("body") or "").strip())
                      and (explicit_session(comment.get("body") or "", args.session_id)
                           or projected_handoff(comment, scoped, actor, args.session_id))]
        candidates = [comment for comment in candidates if comment.get("id") in releases] or candidates
        require(len(candidates) == 1, "handoff",
                f"requires one posted handoff; found {len(candidates)} candidates: {[row.get('html_url') for row in candidates]}; "
                f"examined: {[handoff_refusal(row, scoped, args.session_id, actor) for row in [comment for comment in comments if (comment.get("user") or {}).get("login") == actor][-10:]]}; "
                "verify the source and select its exact --handoff-url")
        selected = candidates[0]["html_url"]
    require(isinstance(selected, str), "handoff", "handoff URL is missing")
    require(operation.state.get("handoff", {}).get("url", selected) == selected,
            "handoff", "journal already records another handoff; preserve its evidence")
    match = release.HANDOFF.fullmatch(selected)
    require(bool(match), "handoff", "handoff URL is malformed")
    proposed = {**entry, "handoff_url": selected}
    try:
        handoff = release.handoff(reader, proposed, actor)
    except ValueError as error:
        raise GateFailed("handoff", f"{selected}: {error}") from error
    require(started is None or claim_release.stamp(handoff["created_at"]) >= started,
            "handoff", "handoff must be created in this launch generation")
    if not explicit_session(handoff.get("body") or "", args.session_id):
        evidence = evidence if evidence is not None else claim_evidence(entry, operation, "handoff_comments")
        require(projected_handoff(handoff, evidence, actor, args.session_id),
                "handoff", handoff_refusal({**handoff, "html_url": selected}, evidence, args.session_id, actor))
    operation.state["handoff"] = {"url": selected, "session_id": args.session_id, "author": actor,
                                  "revision": fingerprint(handoff)}
    save_state(operation.path, operation.state)
    if not retired and entry.get("handoff_url") != selected:
        with closure.store_lock(args.ledger, create=True):
            rows = json.loads(args.ledger.read_text())
            matches = [row for row in rows if row.get("session_id") == args.session_id]
            require(len(matches) == 1 and release.closure_identity(normalize_ledger_entry(args.ledger, matches[0])) == release.closure_identity(entry),
                    "identity", "ledger generation changed before handoff update")
            matches[0]["handoff_url"] = selected
            save_state(args.ledger, rows)
    entry["handoff_url"] = selected


def closeout_mutations_settled(state: dict) -> bool:
    retired = bool(state.get("generation")) and state.get("retired_generation") == state["generation"]
    return all(step.get("complete") is True or retired and gate in {"quit", "close"} and step.get("complete") is False
               for gate, step in state["steps"].items())


def validate_retired_closeout(args: argparse.Namespace, operation: Operation, *, finish_retirement: bool = False,
                             retain_terminal: bool = False) -> None:
    operation.gate = "retirement"
    generation = operation.state.get("generation")
    require(isinstance(generation, str) and bool(generation)
            and (operation.state.get("retired_generation") == generation or retain_terminal),
            "retirement", "journal has not completed retirement of this generation")
    require(closeout_mutations_settled(operation.state),
            "retirement", "closeout has an uncertain mutation")
    with closure.ownership(args.session_id, create=True):
        saved = closure.source_records({args.session_id}).get(args.session_id)
        if not isinstance(saved, dict):
            raise GateFailed("retirement", "retired source has no canonical closure")
        require(saved.get("session_id") == args.session_id, "retirement", "canonical closure native identity changed")
        identity = release.closure_identity(saved)
        require(fingerprint({key: value for key, value in identity.items() if key != "handoff_url"}) == generation,
                "retirement", "canonical closure generation changed")
        handoff = operation.state.get("handoff", {})
        require(bool(identity["handoff_url"]) and handoff.get("url") == identity["handoff_url"]
                and handoff.get("session_id") == args.session_id
                and getattr(args, "handoff_url", identity["handoff_url"]) in {None, identity["handoff_url"]},
                "retirement", "canonical closure handoff differs from the completed journal")
        for pid, _tty, command in session_liveness.inventory():
            argv, bounded = session_liveness.process_arguments(pid, command)
            resumed, _kind, _exact = session_liveness.invocation(argv, bounded=bounded)
            session_liveness.require_source_not_resumed(args.session_id, resumed)
        validate = release.validate_closure if retain_terminal else release.validate_closure_state
        status = validate({**saved, "_transcript_roots": release.transcript_roots(saved["harness"], None, [], saved)},
                          args.supervisor_session)
        require(status["state"] == "turn_ended" and not status["background_tasks"], "verdict", "closed source has unfinished native work")
        with closure.store_lock(args.ledger, create=True):
            rows = iterm_tab.read_launch_ledger(args.ledger)
            active = [normalize_ledger_entry(args.ledger, row) for row in rows if row.get("session_id") == args.session_id]
            require(not active or (finish_retirement or retain_terminal) and len(active) == 1 and release.closure_identity(active[0]) == identity,
                    "retirement", "active source changed or needs normal closeout to finish ledger retirement")
            if active and finish_retirement:
                save_state(args.ledger, [row for row in rows if row.get("session_id") != args.session_id])


def closeout(args: argparse.Namespace, operation: Operation) -> None:
    if operation.state.get("retired_generation"):
        validate_retired_closeout(args, operation, finish_retirement=True)
        return
    entries = [entry for entry in load_ledger(args.ledger) if entry["session_id"] == args.session_id]
    require(len(entries) == 1, "identity", "native session must match exactly one ledger entry or verified retirement")
    entry = entries[0]
    generation = fingerprint({key: value for key, value in release.closure_identity(entry).items() if key != "handoff_url"})
    require(operation.state.get("generation", generation) == generation, "identity", "journal belongs to another launch generation")
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
        while True:
            current = [row for row in load_ledger(args.ledger) if row["session_id"] == args.session_id]
            require(current == [entry], "identity", "active source changed; preserve the ledger")
            try:
                require(session_liveness.process_absent(entry.get("pid")), "process_exit", "process remains; terminal kept")
                native.verify_source_process(entry)
                return
            except (ValueError, OSError, SubprocessError) as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GateFailed("process_exit", str(error)) from error
                time.sleep(min(0.25, remaining))

    with closure.ownership(args.session_id, create=True):
        claims()
        verify_handoff(entry, args, operation, retired=False)
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
        elif not session_liveness.process_absent(entry.get("pid")):
            require(
                operation.state["steps"]["quit"].get("complete") is True,
                "quit",
                "previous send is uncertain; preserve the session for reconciliation",
            )
        wait_for_exit()
    ready(entry, args.supervisor_session)
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
        release.validate_closure({**saved, "_transcript_roots": release.transcript_roots(saved["harness"], None, [], saved)}, args.supervisor_session)
        with closure.store_lock(args.ledger, create=True):
            active_rows = iterm_tab.read_launch_ledger(args.ledger)
            active_matches = [normalize_ledger_entry(args.ledger, row) for row in active_rows if row.get("session_id") == args.session_id]
            require(active_matches == [entry], "retirement", "active source changed; preserve the ledger")
            operation.state["retired_generation"] = generation
            save_state(operation.path, operation.state)
            save_state(args.ledger, [row for row in active_rows if row.get("session_id") != args.session_id])


def main() -> int:
    parser = argparse.ArgumentParser(description="Close one normal finished session, stopping at the first failed gate.")
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--supervisor-session", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--handoff-url", help="explicit posted handoff after source verification, including a different work issue")
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
