#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5", "zstandard==0.25.0"]
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
GH_PLAN = SCRIPTS.parents[1] / "github/scripts/gh-plan.py"
UNRESOLVED_CELLS_DECISION = "https://github.com/cbusillo/codex-skills/issues/3523#issuecomment-6828126999"

if TYPE_CHECKING:
    from skills.github.scripts.github_catalog_health import MARKER as OPERATION_MARKER
    from skills.github.scripts.github_plan_claim import (
        live_lines,
        session_fields,
        status_names_actor,
        unquoted_text,
    )
else:
    from github_catalog_health import MARKER as OPERATION_MARKER
    from github_plan_claim import (
        live_lines,
        session_fields,
        status_names_actor,
        unquoted_text,
    )


def require(condition: bool, gate: str, reason: str) -> None:
    if not condition:
        raise GateFailed(gate, reason)


def accepted_unresolved_claims(entry: dict, operation: Operation, result: dict) -> bool:
    acceptance = operation.state.get("unresolved_cells", {})
    return (acceptance.get("decision") == UNRESOLVED_CELLS_DECISION
            and acceptance.get("report") == fingerprint(result)
            and result.get("session_ids") == [entry["session_id"]])


def unresolved_cell_claims(entry: dict, args: argparse.Namespace, operation: Operation, result: dict) -> dict:
    operation.gate = "claims"
    decision = args.unresolved_cells_decision
    require(
        entry["harness"] == "codex" and not entry.get("recovery"),
        "claims",
        "unresolved-cell retirement requires a normal finished Codex session",
    )
    require(
        decision == UNRESOLVED_CELLS_DECISION,
        "claims",
        "requires the recorded Q1354 decision URL",
    )
    reader = GitHubReader(gh_prefix_args=automation_only_gh_prefix_args(), deadline_at=time.time() + 120)
    actor = github_identity.automation_login()
    require(bool(actor), "identity", "configured automation identity is unknown")
    match = claim_release.COMMENT_URL.fullmatch(decision)
    posted = reader.get_json(f"/repos/{match[1]}/issues/comments/{match[3]}", step="closeout_decision")
    require(
        (posted.get("user") or {}).get("login") == actor
        and bool(posted.get("created_at"))
        and posted.get("created_at") == posted.get("updated_at"),
        "claims",
        "recorded Director decision was changed or is unavailable",
    )
    repo, number = entry.get("repository"), entry.get("issue")
    require(
        isinstance(repo, str) and release.REPO.fullmatch(repo) is not None and type(number) is int and number > 0,
        "claims",
        "requires the session's exact repository and issue",
    )
    unresolved = result.get("unresolved_invocations")
    if not isinstance(unresolved, list):
        raise GateFailed("claims", "audit has no unresolved invocation list")
    require(
        result.get("ok") is False
        and result.get("claims_clear") is False
        and result.get("coverage", {}).get("complete") is False
        and result.get("session_ids") == [entry["session_id"]]
        and result.get("blocked") == []
        and result.get("coverage", {}).get("search_errors") == []
        and not result.get("error")
        and bool(unresolved),
        "claims",
        "the decision accepts only unresolved code-mode cells with every claim released",
    )
    cells = []
    for item in unresolved:
        require(
            item.get("code_mode") is True and bool(item.get("call_id")) and bool(item.get("reason")),
            "claims",
            "unresolved invocation is not an identified code-mode cell with a reason",
        )
        cell = {key: item[key] for key in ("session", "call_id", "reason") if key in item}
        if item.get("refusals"):
            cell["refusals"] = [{key: refusal[key] for key in ("line", "column", "syntax") if key in refusal}
                                for refusal in item["refusals"]]
        cells.append(cell)
    destination = {"repository": repo, "issue": number}
    if operation.state["steps"].get("unresolved_cells_note"):
        posted_cells = operation.state.get("unresolved_cells", {})
        require(posted_cells.get("destination") == destination and all(cell in posted_cells.get("cells", []) for cell in cells),
                "claims", "unresolved cells or their destination changed; preserve the original posted check")
        cells = posted_cells["cells"]
    operation.state["unresolved_cells"] = {"decision": decision, "cells": cells, "destination": destination,
                                            "report": fingerprint(result)}
    save_state(operation.path, operation.state)
    note = operation.evidence / "unresolved-cells.md"
    note.write_text(
        f"Closeout claim check under [Director decision Q1354]({decision}).\n\n"
        f"Session: {entry['session_id']}\n\n"
        "Fresh GitHub inventory and exact release checks confirm every session claim is released. "
        "The strict audit remains incomplete for these code-mode cells:\n\n"
        f"```json\n{json.dumps(cells, indent=2)}\n```\n\n"
        "Only this unresolved-cell outcome is accepted. Retirement still requires every remaining closeout gate.\n"
    )
    return result


def explicit_session(body: str, session: str) -> bool:
    text = re.sub(r"<!--.*?-->", "", unquoted_text(body), flags=re.DOTALL)
    if session_fields(text) and not session_fields(text, current_only=True):
        return False
    return (claim_release.related_sessions_match(text, session)
            and re.search(rf"(?<![\w-]){re.escape(session)}(?![\w-])", text) is not None)


def post_unresolved_cells(entry: dict, operation: Operation) -> None:
    gate = "unresolved_cells_note"
    replaying = gate in operation.state["steps"]
    try:
        operation.run(gate, [str(GH_PLAN.with_name("gh-comment")), "issue", str(entry["issue"]),
                      "--repo", entry["repository"], "--body-file", str(operation.evidence / "unresolved-cells.md")], once=True)
    except GateFailed:
        if replaying:
            raise
        result = json.loads((operation.evidence / (gate + ".json")).read_text())
        if (result.get("exit_code") not in {None, 0}
                and json.loads(result.get("stdout") or "{}").get("write_outcome") == "not_started"):
            operation.state["steps"].pop(gate, None)
            save_state(operation.path, operation.state)
        raise


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

    started = time.monotonic()
    home = Path(entry["codex_home"]).expanduser().resolve(strict=True)
    action = closure.lease_state(entry["session_id"])
    homes = {home, *(Path(value).resolve() for value in closure.action_homes(action)["known"])}
    read = closure.transcript_facts if entry.get("recovery") else closure.transcripts
    snapshots = read(entry["session_id"], entry["transcript"], include_status=True, additional_homes=homes)
    require(not any(row["status"].get("background_tasks") or row["status"].get("active_realtime_sessions")
                    or row["status"].get("realtime_coverage") is False for row in snapshots.values()),
            "native_idle", "an authenticated source copy has background work or incomplete lifecycle coverage")
    paths = native.authenticated_paths(snapshots, observation_homes(strict=True, additional_homes=homes))
    homes.update(Path(value) for value in paths)
    snapshot = {"path": entry["transcript"], "transcripts": snapshots, "home_paths": paths}
    reads = native.history_read_options(snapshots, started, len(homes) + 1)
    for current in homes:
        async with native.connection(current, **reads) as request:
            facts = await native.read_thread_facts(request, entry["session_id"])
        native.require_quiet(facts, native.local_snapshot(snapshot, facts, str(current)), native.home_action(action, str(current)))


def ready(entry: dict, supervisor: str) -> dict:
    require(
        entry.get("supervisor_owned") is True and entry["session_id"] != supervisor,
        "ownership",
        "requires another explicitly Supervisor-owned session",
    )
    if entry.get("recovery"):
        return release.validate_recovery(entry, supervisor,
                                         before_exit=not session_liveness.process_absent(entry.get("pid")))
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
    accepted = receipt_data.get("exit_code") == 1 and accepted_unresolved_claims(entry, operation, checked)
    if (not accepted and (receipt_data.get("exit_code") != 0 or checked.get("ok") is not True
            or checked.get("claims_clear") is not True or checked.get("coverage", {}).get("complete") is not True
            or checked.get("unresolved_invocations") != [])
            or checked.get("session_ids") != [entry["session_id"]]
            or checked.get("blocked") != []):
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
    replacing = entry.get("recovery", {}).get("kind") == "handoff" and getattr(args, "handoff_url", None)
    require(not associated or associated == selected or bool(replacing), "handoff", "current handoff differs; preserve it")
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
    if replacing and associated and associated != selected:
        previous = release.handoff(reader, {**entry, "handoff_url": associated}, actor)
        require(claim_release.stamp(handoff["created_at"]) > claim_release.stamp(previous["created_at"]),
                "handoff", "recovery handoff does not supersede the recorded handoff")
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


def handoff_successor(entry: dict, args: argparse.Namespace) -> str | None:
    recovery = entry.get("recovery", {}).get("kind")
    selected = entry["recovery"]["successor_session"] if recovery == "handoff" else getattr(args, "successor_session", None)
    if recovery == "gone" or not entry.get("worker_cap"):
        require(not selected or bool(recovery), "successor", "only a lead's handoff moves ownership to a successor")
        return None
    if not selected and not entry.get("launch_started_at"):
        return None
    rows = load_ledger(args.ledger)
    active = {row["session_id"] for row in rows}
    candidates = [row for row in release.successor_candidates(entry, rows, selected)
                  if row.get("worker_cap") and row["session_id"] in active]
    if not candidates and (recovery or not selected):
        return None
    require(len(candidates) == 1, "successor",
            "a handed-off lead needs one active matching successor lead; select --successor-session")
    try:
        release.require_successor(entry, candidates[0])
    except ValueError as error:
        raise GateFailed("successor", str(error)) from error
    return candidates[0]["session_id"]


def adopt_workers(ledger: Path, source: str, successor: str) -> None:
    with closure.store_lock(ledger, create=True):
        rows = json.loads(ledger.read_text())
        leads = [row for row in rows if row.get("session_id") == source]
        require(len(leads) == 1 and not leads[0].get("worker_launches"), "successor",
                "source lead has unsettled worker launches; recover them before its handoff closes")
        require(any(row.get("session_id") == successor for row in rows), "successor",
                "successor left the active ledger; select a current successor")
        workers = [row for row in rows if row.get("lead_session_id") == source]
        for row in workers:
            row["lead_session_id"] = successor
        if workers:
            save_state(ledger, rows)


def status_records_naming(finish_evidence: Path, session: str) -> list[dict]:
    try:
        output = json.loads(json.loads(finish_evidence.read_text())["stdout"])
        return [row for report in output["reports"] if report.get("session_id") == session
                for row in report.get("retained_status_claims") or []]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def repoint_status_actors(operation: Operation, source: str, successor: str) -> bool:
    reader = GitHubReader(gh_prefix_args=automation_only_gh_prefix_args(), deadline_at=time.time() + 120)
    moved = False
    for row in status_records_naming(operation.evidence / "finish.json", source):
        repo, number = row["repository"], row["issue"]
        issue = reader.get_json(f"/repos/{repo}/issues/{number}", step="repoint_status")
        if not status_names_actor(unquoted_text(issue.get("body") or ""), source):
            continue
        operation.run(f"repoint-{repo.replace('/', '-')}-{number}",
                      ["uv", "run", str(GH_PLAN), "--repo", repo, "update-section", str(number), "Current Status",
                       "--next-actor", f"agent:{successor}"], once=True)
        moved = True
    return moved


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
        require(saved.get("closure_kind") == "claim_free_recovery" or
                bool(identity["handoff_url"]) and handoff.get("url") == identity["handoff_url"]
                and handoff.get("session_id") == args.session_id
                and getattr(args, "handoff_url", identity["handoff_url"]) in {None, identity["handoff_url"]},
                "retirement", "canonical closure handoff differs from the completed journal")
        for pid, _tty, command in session_liveness.inventory():
            argv, bounded = session_liveness.process_arguments(pid, command)
            resumed, _kind, _exact = session_liveness.invocation(argv, bounded=bounded)
            session_liveness.require_source_not_resumed(args.session_id, resumed)
        validate = release.validate_finished if saved.get("closure_kind") == "claim_free_recovery" else (
            release.validate_closure if retain_terminal else release.validate_closure_state)
        status = validate({**saved, "_transcript_roots": release.transcript_roots(saved["harness"], None, [], saved)},
                          args.supervisor_session)
        require((saved.get("closure_kind") == "claim_free_recovery" or status["state"] == "turn_ended")
                and not status["background_tasks"], "verdict", "closed source has unfinished native work")
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
    if getattr(args, "recover_ended", False):
        require(closeout_mutations_settled(operation.state), "recovery", "previous mutation requires reconciliation")
        entry["recovery"] = ({"kind": "gone"} if session_liveness.process_absent(entry.get("pid")) else {
            "kind": "handoff", "ledger": str(args.ledger.resolve()),
            "successor_session": getattr(args, "successor_session", None),
            "handoff_url": getattr(args, "handoff_url", None)})
        saved = closure.source_records({args.session_id}).get(args.session_id)
        if isinstance(saved, dict) and saved.get("closure_kind") == "claim_free_recovery":
            entry["recovery"] = saved["recovery"]
    ready(entry, args.supervisor_session)
    successor = handoff_successor(entry, args)
    backend = entry.get("backend", "iterm")
    selected_terminal = entry.get("tmux_session_id" if backend == "tmux" else "iterm_session_id")
    if (not isinstance(selected_terminal, str) or not selected_terminal) and entry.get("recovery", {}).get("kind") != "gone":
        raise GateFailed("identity", "thread-only session has no matched terminal")
    terminal_id = selected_terminal or ""
    terminal = ["uv", "run", str(SCRIPTS / "iterm_tab.py"), "--backend", backend]

    def call(gate: str, action: str, *extra: str, once: bool = False) -> dict:
        return operation.run(gate, [*terminal, action, "--session-id", terminal_id, *extra], once=once)

    def claims() -> dict:
        decision = getattr(args, "unresolved_cells_decision", None)
        try:
            result = operation.run("claims", [
                "uv", "run",
                str(SCRIPTS.parents[1] / "work-closeout/scripts/check_claims.py"),
                "--owner", args.owner,
                "--session", args.session_id,
                "--transcript", entry["transcript"],
                *(["--require-released-claims"] if entry.get("recovery") or decision else []),
                *(["--inventory-owner", args.owner] if decision else []),
            ], timeout=660)
        except GateFailed:
            if not decision:
                raise
            audited = json.loads((operation.evidence / "claims.json").read_text())
            require(audited.get("exit_code") == 1, "claims", "unresolved-cell decision needs a completed claims audit")
            result = unresolved_cell_claims(entry, args, operation, json.loads(audited["stdout"]))
        require(
            accepted_unresolved_claims(entry, operation, result)
            or result.get("claims_clear") is True and result.get("coverage", {}).get("complete") is True,
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
        return result

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
            expected = {key: value for key, value in entry.items() if key != "recovery"}
            require(current == [expected], "identity", "active source changed; preserve the ledger")
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
        claim_report = claims()
        if not entry.get("recovery") or entry["recovery"]["kind"] == "handoff":
            verify_handoff(entry, args, operation, retired=False)
        if accepted_unresolved_claims(entry, operation, claim_report):
            post_unresolved_cells(entry, operation)
        if successor:
            adopt_workers(args.ledger, args.session_id, successor)
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
    require(all(step.get("complete") is True for gate, step in operation.state["steps"].items() if gate.startswith("repoint-")),
            "repoint", "a Current Status re-point is uncertain; read that issue back before rerunning")
    finish_command = [
            "uv", "run",
            str(SCRIPTS / "release_session_claims.py"),
            "finish",
            "--ledger", str(args.ledger),
            "--session-id", args.session_id,
            "--supervisor-session", args.supervisor_session,
            "--owner", args.owner,
            "--confirm-session-ended", "--apply",
            *(["--recover-ended"] if entry.get("recovery") else []),
            *(["--successor-session", entry["recovery"]["successor_session"]]
              if entry.get("recovery", {}).get("kind") == "handoff" else []),
            *(["--handoff-url", entry["handoff_url"]] if entry.get("recovery") and entry.get("handoff_url") else []),
    ]
    try:
        finish = operation.run("finish", finish_command, timeout=660)
    except GateFailed:
        if not (successor and repoint_status_actors(operation, args.session_id, successor)):
            raise
        finish = operation.run("finish", finish_command, timeout=660)
    require(
        finish.get("ok") is True and finish.get("needs_reconciliation") is False,
        "finish",
        "claim discovery or release needs reconciliation; terminal kept",
    )
    panes = operation.run("terminals", [*terminal, "list"]) if terminal_id else []
    if any(pane["session_id"] == terminal_id for pane in panes):
        call("close", "close", "--ledger", str(args.ledger), once=True)
    panes = operation.run("disappearance", [*terminal, "list"]) if terminal_id else []
    require(not any(pane["session_id"] == terminal_id for pane in panes), "disappearance", "terminal closure is uncertain")
    operation.gate = "retirement"
    with closure.ownership(args.session_id, create=True):
        saved = closure.source_records({args.session_id})[args.session_id]
        require(not saved.get("resumed_at") and release.closure_identity(saved) == release.closure_identity(entry),
                "retirement", "canonical closure does not preserve this finished generation")
        release.validate_finished({**saved, "_transcript_roots": release.transcript_roots(saved["harness"], None, [], saved)}, args.supervisor_session)
        with closure.store_lock(args.ledger, create=True):
            active_rows = iterm_tab.read_launch_ledger(args.ledger)
            active_matches = [normalize_ledger_entry(args.ledger, row) for row in active_rows if row.get("session_id") == args.session_id]
            require(active_matches == [{key: value for key, value in entry.items() if key != "recovery"}],
                    "retirement", "active source changed; preserve the ledger")
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
    parser.add_argument("--unresolved-cells-decision", metavar="URL",
                        help="Apply Q1354 only to unresolved code-mode cells after fresh exact claim-release checks")
    parser.add_argument("--recover-ended", action="store_true", help="Recover a claim-free gone or proven handed-off generation")
    parser.add_argument("--successor-session", help="Exact successor ledger row of a handed-off lead; its workers and Current Status actors move to it")
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
