#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
"""Record verified session closure and release its automation claims. Dry run by default."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
import json
import fcntl
import os
from pathlib import Path
from typing import Any
import re
import subprocess
import sys
import tempfile
import time

import session_record
import session_liveness
import account_choice
import backfill_inventory
from session_liveness import inventory

GITHUB = Path(__file__).resolve().parents[2] / "github/scripts"
sys.path.insert(0, str(GITHUB))
import github_identity
import github_plan_claim as claim
import github_plan_release as release
import github_plan_closure as closure
from github_read import GitHubReader, GitHubReadError, GitHubReadShapeError, automation_only_gh_prefix_args
from github_comment import CommentError, comment

REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
HANDOFF = release.COMMENT_URL


class PartialRelease(ValueError):
    def __init__(self, error, report):
        super().__init__(str(error))
        self.report = report


class ConfirmedIssueRefusal(ValueError):
    def __init__(self, payload):
        super().__init__(payload.get("error", "confirmed issue release refusal"))
        self.payload = payload


class OwnershipDiagnostic(ValueError):
    """An ownership refusal already attributable to one canonical comment."""
    def __init__(self, claim_comment: int, message: str):
        super().__init__(message)
        self.claim_comment = claim_comment
        self.source_groups = []


def verify_process_exit(entry, *, ended_at=None):
    def source_record():
        return closure.source_records({entry["session_id"]}).get(entry["session_id"])
    return _verify_process_exit(entry, ended_at, source_record)


def _verify_process_exit(entry, ended_at, source_record):
    if not entry.get("_inventory") and (
        type(entry.get("pid")) is not int or entry["pid"] <= 0
        or not isinstance(entry.get("tty"), str) or not entry["tty"]
    ):
        raise ValueError("closure requires the matched process PID and TTY")
    cutoff = release.stamp(entry["tty_exited_at"]) if entry.get("tty_exited_at") else ended_at
    if cutoff is not None and ended_at is not None and cutoff > ended_at:
        raise ValueError("native TTY exit cutoff is newer than closure")
    rows = inventory()
    replacement = None
    expected = entry.get("_tty_owner")
    if expected is not None:
        owner = session_liveness.replacement_tty_owner(entry, rows, started_at=closure.process_started_at)
        if owner is None or owner[:2] != expected or owner[1] != cutoff:
            raise ValueError("replacement TTY owner changed; preserve claims")
        replacement = lambda pid: pid in owner[2]
        # ps births have second precision. The proved root and its tree may
        # share that second; ancestry, not a rounded time, identifies them.
        cutoff -= timedelta(microseconds=1)

    def independent(pid, argv):
        if closure.guarded_launch_endpoint(pid, argv) is None:
            return False
        # Retain the existing saved-generation identity check. A first or
        # resumed generation instead gets its cutoff from the native pane.
        saved = source_record()
        if saved is not None and not saved.get("resumed_at") and saved.get("process_exit_verified"):
            identity = lambda row: {key: value for key, value in session_record.normalize_ledger_entry(
                closure.closed_log(), row).items() if not key.startswith("_") and key != "handoff_url"}
            return identity(saved) == identity(entry)
        return True

    def verify(cutoff, replacement):
        return session_liveness.verify_process_exit(
            entry, rows=rows, ended_at=cutoff, started_at=closure.process_started_at,
            independent_harness=independent, replacement_owner=replacement)
    try:
        return verify(cutoff, replacement)
    except ValueError:
        if expected is not None or cutoff is None:
            raise
        # A saved replacement cutoff may equal its non-idle root's rounded
        # birth. Re-prove ancestry only when the existing strict check needs it.
        owner = session_liveness.replacement_tty_owner(entry, rows, started_at=closure.process_started_at)
        if owner is None or owner[1] != cutoff:
            raise
        return verify(cutoff - timedelta(microseconds=1), lambda pid: pid in owner[2])


def verify_closed(entry, supervisor_session, *, ended_at, native=None, process=None):
    historical = entry.get("_inventory") is True
    if entry["session_id"] == supervisor_session or (
        not historical and entry.get("supervisor_owned") is not True
    ):
        raise ValueError("requires another explicitly Supervisor-owned session")
    if historical and (entry.get("safe_exit_yes") is not True or entry.get("still_running") is not False):
        raise ValueError("inventory closure requires the Supervisor's verified safe exit and inactivity")
    cutoff = release.stamp(entry["tty_exited_at"]) if entry.get("tty_exited_at") else ended_at
    if cutoff is not None and ended_at is not None and cutoff > ended_at:
        raise ValueError("native TTY exit cutoff is newer than closure")
    if process is None:
        verify_process_exit(entry, ended_at=cutoff)
    else:
        import github_plan_native
        github_plan_native.recheck_source_process(process)
    if native is None:
        closure.require_fresh_action_verdict(entry)
    else:
        import github_plan_native
        github_plan_native.require_proof_quiet(native)
    status = session_record.session_status(entry)
    session_liveness.require_safe_status(status)
    return status


def validate_closure(entry, supervisor_session, *, native=None, process=None):
    if entry.get("resumed_at"):
        raise ValueError("closed generation has resumed; record the new launch generation")
    if entry.get("process_exit_verified") is not True:
        raise ValueError("closed-session log lacks verified process exit")
    if not isinstance(entry.get("ended_at"), str) or re.search(r"\s", entry["ended_at"]):
        raise ValueError("closure requires a single timezone-qualified ISO timestamp")
    ended = release.stamp(entry["ended_at"])
    if ended > datetime.now(timezone.utc):
        raise ValueError("closure timestamp is in the future")
    private = entry.get("_closure_source") if process is None else None
    if private is not None:
        private_path = Path(private["path"])
        original, saved = _saved_closure(private_path, entry["session_id"])
        if saved != private["source"]:
            raise ValueError("private closure source changed after recording")
        import github_plan_native
        process = github_plan_native._source_process(entry, saved, recorded=entry)
    status = verify_closed(entry, supervisor_session, ended_at=ended, native=native, process=process)
    if not status["timestamp"] or release.stamp(status["timestamp"]) > ended:
        raise ValueError("transcript resumed after the recorded closure")
    if "verdict_at" in entry and entry["verdict_at"] != status["timestamp"]:
        raise ValueError("recorded verdict time differs from the verified transcript")
    paths = {Path(entry["transcript"]).resolve()}
    for root in entry.get("_transcript_roots", []):
        paths.update(p.resolve() for p in Path(root).rglob(f"*{entry['session_id']}*.jsonl"))
    for path in paths:
        other = session_record.session_status({**entry, "transcript": str(path)})
        records = session_record.read_jsonl(path)
        if not other["safe_verdict"] or any(
            release.stamp(r["timestamp"]) > ended for r in records if r.get("timestamp")
        ):
            raise ValueError("source resumed or has newer activity in a configured native transcript")
        if session_record.session_status({**entry, "transcript": str(path)}) != other:
            raise ValueError("native transcript changed during closure verification")
    # Optional host/boot/account fingerprints belong to idle admission only.
    # Ordinary release still revalidates exit and every required native copy.
    if private is not None and _saved_closure(private_path, entry["session_id"])[0] != original:
        raise ValueError("private closure source changed during verification")
    return status


def transcript_roots(harness, configured, explicit):
    if explicit:
        return [str(p.expanduser().resolve()) for p in explicit]
    config = account_choice.load_config(configured)
    roots = []
    for account in config["accounts"]:
        if account["provider"] == ("openai" if harness == "codex" else "anthropic"):
            variable = "CODEX_HOME" if harness == "codex" else "CLAUDE_CONFIG_DIR"
            home = Path(account["env"].get(variable, str(Path.home() / ".claude"))).expanduser()
            roots.extend([str(home / "sessions"), str(home / "archived_sessions")] if harness == "codex" else [str(home / "projects")])
    if not roots:
        raise ValueError("no configured source harness homes; supply --transcript-root for its known native transcript directories")
    return list(dict.fromkeys(roots))


@contextmanager
def _closure_guard(path, entry, *, create=False):
    shared = path.resolve() == closure.closed_log().resolve()
    coordination = closure.ownership(entry["session_id"], create=create) if shared else nullcontext()
    with coordination as held:
        saved = closure.source_records({entry["session_id"]}).get(entry["session_id"]) if shared else None
        if saved and saved.get("resumed_generation"):
            generation = saved["resumed_generation"]
            if any(entry.get(key) != generation.get(key) for key in (
                    "launch_pid", "launch_generation", "launch_started_at", "pid", "tty", "codex_home")):
                raise ValueError("finish needs the new launch metadata from codex-session.py status")
        yield shared, saved, held


def record_closure(path, entry, supervisor_session, *, persist=True):
    with _closure_guard(path, entry, create=persist) as (shared, saved, _held):
        native = None
        if shared and entry.get("harness") == "codex" and (closure.action_pending(entry["session_id"])
                or saved and saved.get("resumed_at") and not saved.get("resumed_generation")):
            import github_plan_native
            native = asyncio.run(github_plan_native.prove_end(entry))
            if persist:
                github_plan_native.resolve_pending(entry["session_id"], native)
        closed, source = _record_closure(path, entry, supervisor_session, persist=persist, native=native)
        if not shared:
            # Keep the private identity, never a process proof, for later boundaries.
            closed["_closure_source"] = {"path": str(path.resolve()), "source": source}
        if native is not None and not persist:
            # Keep only the source/action identities for this read-only preview.
            # The native proof itself never leaves the ownership guard.
            closed["_finish_native"] = {"source": native["snapshot"]["source"], "action": native["action"]}
        return closed


def _validate_finish(entry, supervisor_session, *, apply):
    anchor = entry.get("_finish_native") if not apply else None
    if anchor is None:
        return validate_closure(entry, supervisor_session)
    with _closure_guard(closure.closed_log(), entry) as (_shared, saved, held):
        if not held:
            raise ValueError("native finish preview lost its ownership lease")
        if saved != anchor["source"] or closure.lease_state(entry["session_id"]) != anchor["action"]:
            raise ValueError("native action or source identity changed after finish preview")
        import github_plan_native
        process = github_plan_native._source_process(entry, saved, recorded=entry)
        native = asyncio.run(github_plan_native.prove_end(entry, process=process))
        if native["snapshot"]["source"] != anchor["source"] or native["action"] != anchor["action"]:
            raise ValueError("native proof belongs to a different finish source or action")
        status = validate_closure(entry, supervisor_session, native=native, process=native["snapshot"]["process"])
        github_plan_native._require_current_proof(entry["session_id"], native)
        return status


def _closure_identity(entry):
    identity = {key: entry.get(key) for key in (
        "pid", "tty", "harness", "supervisor_owned", "handoff_url", "transcript", "codex_home",
        "launch_pid", "launch_generation", "launch_started_at",
        "tmux_session_id", "tmux_socket", "window_id", "tab_id", "iterm_session_id")}
    for key in ("transcript", "codex_home"):
        if identity[key]:
            identity[key] = str(Path(identity[key]).expanduser().resolve())
    if isinstance(identity["tty"], str):
        identity["tty"] = identity["tty"].removeprefix("/dev/")
    return identity


closure_identity = _closure_identity


def _saved_closure(path, session, *, create=False):
    """Read the recorder's selected saved identity under its store lock."""
    with closure.store_lock(path, create=create):
        original = closure.selected_source_rows(json.loads(path.read_text()), session) if path.exists() else []
    return original, session_record.normalize_ledger_entry(path, original[0]) if original else None


def _record_closure(path, entry, supervisor_session, *, persist=True, native=None):
    """Return closure and saved identity; preserve the actual end time on retry."""
    original, saved = _saved_closure(path, entry["session_id"], create=persist)
    existing = [saved] if saved is not None else []
    import github_plan_native
    if native is not None:
        process = native["snapshot"]["process"]
        if saved != process.saved:
            raise ValueError("source generation changed before recording closure")
    else:
        process = github_plan_native._source_process(entry, saved)
        github_plan_native.recheck_source_process(process)
    recovered = False
    if existing and existing[0].get("resumed_at") and not existing[0].get("resumed_generation") and native:
        # An invalidation is not a launch. Full current native/source proof can
        # recover an aborted prelaunch without inventing a process generation.
        later = any(status["status"]["turn_completed_at"] and
                    release.stamp(status["status"]["turn_completed_at"]) > release.stamp(existing[0]["ended_at"])
                    for status in native["snapshot"]["transcripts"].values())
        if later:
            if _closure_identity(entry) != _closure_identity(existing[0]):
                raise ValueError("finish needs the new launch metadata from codex-session.py status")
            entry = {**existing[0], **entry}
            for key in ("ended_at", "verdict_at", "resumed_at", "closure_evidence"):
                entry.pop(key, None)
            existing = []
        else:
            existing[0] = {k: v for k, v in existing[0].items() if k != "resumed_at"}
            recovered = True
    if existing and existing[0].get("resumed_at"):
        resumed = existing[0].get("resumed_generation") or {}
        if not resumed or any(entry.get(k) != resumed.get(k) for k in (
            "launch_pid", "launch_generation", "launch_started_at", "pid", "tty", "codex_home"
        )):
            raise ValueError("finish needs the new launch metadata from codex-session.py status")
        existing = []
    if existing:
        saved_identity, current_identity = _closure_identity(existing[0]), _closure_identity(entry)
        if any(saved_identity[key] != current_identity[key] for key in (
                "pid", "tty", "harness", "handoff_url", "supervisor_owned", "transcript")):
            raise ValueError("closure log identity changed; preserve the original record")
        closed = {**existing[0], **{k: v for k, v in entry.items() if k.startswith("_")}}
        validate_closure(closed, supervisor_session, native=native, process=process)
        with closure.store_lock(path):
            current = json.loads(path.read_text())
            if closure.selected_source_rows(current, entry["session_id"]) != original:
                raise ValueError("source closure changed during verification; preserve the current generation")
            if recovered and persist:
                # Preserve the actual original end time. An optional admission
                # fingerprint can be recomputed later; never restore the stale one.
                durable = {key: value for key, value in closed.items() if not key.startswith("_")}
                closure.replace_rows([durable if row.get("session_id") == entry["session_id"] else row for row in current])
        return closed, ({k: v for k, v in closed.items() if not k.startswith("_")} if recovered and persist else saved)
    # Keep the generation's verified process cutoff even when a later turn
    # replaces its closure time. Native proof may have outlived the dead pane.
    entry = {k: v for k, v in entry.items() if k != "tty_exited_at"}
    cutoff = process.cutoff
    if cutoff is not None:
        entry = {**entry, "tty_exited_at": cutoff.isoformat()}
    status = session_record.session_status(entry)
    closed = {**entry, "ended_at": datetime.now(timezone.utc).isoformat(),
              "verdict_at": status["timestamp"], "process_exit_verified": True}
    if entry.get("harness") == "codex" and entry.get("codex_home"):
        try:
            closed["closure_evidence"] = closure.evidence(closed)
        except (ValueError, OSError, ImportError, subprocess.SubprocessError):
            # Optional admission proof must not stop the existing release route.
            closed["closure_proof_unavailable"] = True
    validate_closure(closed, supervisor_session, native=native, process=process)
    if not persist:
        return closed, saved
    # Slow native/process/transcript verification owns only this source. The
    # shared store lock covers a fresh read/compare/write, preserving launches
    # and closures published for other sources while verification was running.
    with closure.store_lock(path, create=True):
        current = json.loads(path.read_text()) if path.exists() else []
        if closure.selected_source_rows(current, entry["session_id"]) != original:
            raise ValueError("source closure changed during verification; preserve the current generation")
        retained = [r for r in current if r.get("session_id") != entry["session_id"]]
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump([*retained, {k: v for k, v in closed.items() if not k.startswith("_")}], stream)
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return closed, session_record.normalize_ledger_entry(path, {k: v for k, v in closed.items() if not k.startswith("_")})


def scan_start(entries):
    timestamps = [release.stamp(r["timestamp"]) for e in entries
                  for r in (e["_transcript_records"] if "_transcript_records" in e else
                            session_record.read_jsonl(Path(e["transcript"]))) if r.get("timestamp")]
    if not timestamps:
        raise ValueError("transcripts have no timestamped history for discovery")
    return (min(timestamps) - timedelta(seconds=1)).isoformat()


def belongs_to_session(body, session):
    if session not in body:
        return False
    return any(r["session"] == session for r in claim.records(body)) or (
        session in body and claim.has_ownership_assertion(body)
        and release.related_sessions_match(body, session)
    )


def discover(reader, owner, entries, repos=None):
    """Complete paged comment inventory, including closed issues, since session start."""
    if repos is None:
        repositories = reader.paged_json("/installation/repositories", collection_key="repositories",
                                         step_prefix="claim_repositories")
        repos = [r["full_name"] for r in repositories
                 if r["full_name"].split("/")[0].casefold() == owner.casefold()]
    if not all(REPO.fullmatch(r) and r.split("/")[0].casefold() == owner.casefold() for r in repos):
        raise ValueError("repository scope must belong to the configured owner")
    if not repos:
        raise ValueError("repository inventory contains no repositories for this owner; verify scope and installation coverage")
    sessions = {e["session_id"] for e in entries}
    since = scan_start(entries)
    issues = set()
    for repo in repos:
        comments = reader.paged_json(f"/repos/{repo}/issues/comments", step_prefix="session_claim_inventory",
                                     params={"since": since}, required_query_params={"since": since})
        for c in comments:
            body = c.get("body") or ""
            if not any(s in body for s in sessions):
                continue
            try:
                matching = any(belongs_to_session(body, s) for s in sessions)
            except ValueError as error:
                raise ValueError(f"unreadable claim at {c.get('html_url')}: {error}") from error
            if matching:
                match = re.fullmatch(rf"https://api\.github\.com/repos/{re.escape(repo)}/issues/([1-9]\d*)", c["issue_url"])
                if not match:
                    raise ValueError("claim comment has an unexpected canonical issue URL")
                issues.add((repo, int(match[1])))
        statuses = reader.paged_json(f"/repos/{repo}/issues", step_prefix="session_status_inventory",
                                     params={"state": "all", "since": since}, required_query_params={"since": since})
        for issue in statuses:
            if "pull_request" not in issue and any(belongs_to_session(issue.get("body") or "", s) for s in sessions):
                issues.add((repo, issue["number"]))
    return sorted(issues), repos


def verified_comments(reader, comments):
    def evidence(repo, number, identifier):
        try:
            return reader.get_json(f"/repos/{repo}/issues/comments/{identifier}", step="release_evidence")
        except GitHubReadError as error:
            if getattr(error.result, "status", None) not in {403, 404}:
                raise
            return None
    return release.verified_comments(comments, evidence)


def active_claims(reader, repo, number, session, actor, *, comments=None, verified=False):
    if comments is None:
        comments = reader.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="session_claim_discussion")
    if not verified:
        comments = verified_comments(reader, comments)
    conflicts, _ = claim.discussion_evidence("", comments, {
        "session": "", "worker": "", "branch": "", "claimed_at": ""
    })
    active_ids = {c["id"] for c in conflicts if c.get("source") == "comment"}
    sources = []
    diagnostic = None
    for c in comments:
        records = claim.records(c.get("body") or "")
        if (c["id"] in active_ids and not records
                and belongs_to_session(c.get("body") or "", session)
                and not any(r["session"] == session and re.match(
                    rf"Claimed by {re.escape(r['worker'])}(?:\s|$)", c.get("body") or "")
                    for source in comments if source["id"] in active_ids
                    for r in claim.records(source.get("body") or ""))):
            # The existing release route requires a structured source. Never
            # invent one or release an older hand-written source by worker alias.
            if diagnostic is None:
                diagnostic = OwnershipDiagnostic(c["id"], f"hand-written claim {c['id']} needs its authored exact-ID release")
        if c["id"] in active_ids and any(r["session"] == session for r in records):
            if (c.get("user") or {}).get("login") != actor:
                if diagnostic is None:
                    diagnostic = OwnershipDiagnostic(c["id"], "session has a claim held by another identity; report without releasing")
                continue
            record = release.source_record(c)
            related = [r["id"] for r in comments if r["id"] != c["id"] and r["id"] in active_ids
                       and not claim.records(r.get("body") or "")
                       and re.match(rf"Claimed by {re.escape(record['worker'])}(?:\s|$)", r.get("body") or "")
                       and (r.get("user") or {}).get("login") == actor
                       and re.search(rf"(?m)^Session: {re.escape(session)}[ \t]*$", r.get("body") or "")]
            unresolved = [r["id"] for r in comments if r["id"] in active_ids and r["id"] not in related
                          and not claim.records(r.get("body") or "")
                          and re.match(rf"Claimed by {re.escape(record['worker'])}(?:\s|$)", r.get("body") or "")
                          and (r.get("user") or {}).get("login") == actor
                          and (belongs_to_session(r.get("body") or "", session)
                               or not re.search(r"(?im)^Session(?:[ _-]*ID)?:", r.get("body") or ""))]
            sources.append((c, related, unresolved))
    if diagnostic is not None:
        diagnostic.source_groups = sources
        raise diagnostic
    return sources


def handoff(reader, entry, actor):
    match = HANDOFF.fullmatch(entry.get("handoff_url") or "")
    if not match:
        raise ValueError("requires an exact transcript-verified issue handoff URL")
    evidence = reader.get_json(f"/repos/{match[1]}/issues/comments/{match[3]}", step="session_handoff")
    if ((evidence.get("user") or {}).get("login") != actor
            or evidence.get("issue_url") != f"https://api.github.com/repos/{match[1]}/issues/{match[2]}"):
        raise ValueError("handoff identity or canonical issue differs")
    if entry.get("_inventory") and (
            release.stamp(evidence.get("updated_at") or evidence.get("created_at")) > release.stamp(entry["ended_at"])
            or not release.related_sessions_match(evidence.get("body") or "", entry["session_id"])):
        raise ValueError("inventory handoff changed after closure or names another native session; preserve the original inventory and use the source's evidence-backed release-claim route")
    return evidence


def inventory_source_context(reader, entry, actor):
    """Use the selected canonical ownership record for explicit historical backfill."""
    if not entry.get("_backfill") or not entry.get("_inventory"):
        raise ValueError("original ownership context requires explicit inventory backfill")
    for row in entry.get("_inventory_claims", []):
        comments = reader.paged_json(f"/repos/{row['repo']}/issues/{row['issue']}/comments", step_prefix="backfill_source_context")
        source = next((c for c in comments if c.get("id") == row["claim_comment_id"]), None)
        if source is None or (source.get("user") or {}).get("login") != actor:
            continue
        parsed = claim.records(source.get("body") or "")
        if (len(parsed) == 1 and parsed[0]["session"] == entry["session_id"]
                and release.stamp(source.get("updated_at") or source.get("created_at")) <= release.stamp(entry["ended_at"])):
            url = f"https://github.com/{row['repo']}/issues/{row['issue']}#issuecomment-{source['id']}"
            checked = handoff(reader, {**entry, "handoff_url": url}, actor)
            if checked != source:
                raise ValueError("original ownership context changed during canonical readback")
            return {**entry, "handoff_url": url,
                    "_handoff_provenance": "original native ownership record; closeout verified separately"}, checked
    raise ValueError("no canonical original ownership record matches this inventory session")


def run_release(repo, number, source_id, related, prs, evidence_url, supervisor_session, checkout_root, dry_run):
    checkout = checkout_root / repo.split("/")[1]
    argv = ["uv", "run", str(GITHUB / "gh-plan.py"), "--repo", repo, "release-claim", str(number),
            "--claim-comment", str(source_id), "--evidence-comment", evidence_url,
            "--role", "supervisor", "--session", supervisor_session, "--confirm-session-ended"]
    for value in related:
        argv += ["--related-claim-comment", str(value)]
    for value in prs:
        argv += ["--retained-pr", value]
    if dry_run:
        argv.append("--dry-run")
    result = subprocess.run(argv, cwd=checkout, capture_output=True, text=True, timeout=180)
    payload = json.loads(result.stdout)
    if result.returncode or payload.get("ok") is not True or payload.get("outcome_certainty") not in {"confirmed", "reconciled_applied"}:
        if dry_run and payload.get("release_refusal_scope") == release.ISSUE_REFUSAL_SCOPE and "post_release" not in payload.get("completed_steps", []):
            raise ConfirmedIssueRefusal(payload)
        raise ValueError(f"release-claim did not confirm success; preserve records: {json.dumps(payload)}")
    return payload


def release_session(reader, entry, actor, supervisor_session, issues, checkout_root, apply):
    reader.deadline_at = time.time() + entry.get("_read_deadline_seconds", 600)
    if entry.get("_finish"):
        status = _validate_finish(entry, supervisor_session, apply=apply)
        completed = finished_claims(reader, entry, actor, issues, apply)
        if _validate_finish(entry, supervisor_session, apply=apply) != status:
            raise ValueError("source transcript changed; preserve claims")
        return completed
    status = validate_closure(entry, supervisor_session)
    if entry.get("handoff_url"):
        handoff_record = handoff(reader, entry, actor)
    else:
        entry, handoff_record = inventory_source_context(reader, entry, actor)
    plans = []
    retained_status = []
    entry["_releaser_session"] = supervisor_session
    entry["_pr_claim_handoffs"] = {}
    pulls_by_repo = {}
    handoff_prs = set(re.findall(r"https://github\.com/[^/\s]+/[^/\s]+/pull/[1-9]\d*", handoff_record.get("body") or ""))
    refused = []
    for repo, number in issues:
        if entry.get("_inventory") and (repo, number) not in {(r["repo"], r["issue"]) for r in entry["_inventory_claims"]}:
            continue
        try:
            issue_plans, status_claims = plan_issue(reader, repo, number, entry, actor, handoff_prs, pulls_by_repo, checkout_root)
            plans.extend(issue_plans)
            retained_status.extend(status_claims)
        except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError, GitHubReadError, GitHubReadShapeError) as error:
            refused.append({"repository": repo, "issue": number, "error": str(error)})
    report = {"session_id": entry["session_id"], "claims": [], "dry_run": not apply,
              "retained_status_claims": retained_status, "issue_refusals": refused,
              "inventory_claims": list(entry.get("_inventory_results", {}).values()),
              "handoff_url": entry["handoff_url"],
              "pr_claim_handoffs": list(entry.get("_pr_claim_handoffs", {}).values()),
              "closure_evidence": "Supervisor verified inventory; original PID/TTY unavailable" if entry.get("_inventory") else "matched PID/TTY log"}
    try:
        return apply_plans(entry, supervisor_session, plans, status, checkout_root, actor, apply, report, reader)
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError, CommentError) as error:
        raise PartialRelease(error, report) from error


def finished_claims(reader, entry, actor, issues, apply):
    """Consume one canonical release proof per source; never manufacture a release."""
    released, refused, retained_status = [], [], []
    ended = release.stamp(entry["ended_at"])
    for repo, number in issues:
        reader.deadline_at = time.time() + entry.get("_read_deadline_seconds", 600)
        identity = {"repository": repo, "issue": number}
        try:
            issue = reader.get_json(f"/repos/{repo}/issues/{number}", step="finished_claim_issue")
            comments = verified_comments(reader, reader.paged_json(
                f"/repos/{repo}/issues/{number}/comments", step_prefix="finished_claim_history"))
            def source_refusal(source, message):
                return {**identity, "claim_comment": source["id"], "error": message}
            sources = [c for c in comments if belongs_to_session(c.get("body") or "", entry["session_id"])
                       and (claim.records(c.get("body") or "") or re.match(
                           r"Claimed by:?\s+\S+", claim.ownership_text(c.get("body") or "", strip_quotes=False), re.IGNORECASE))]
            if belongs_to_session(issue.get("body") or "", entry["session_id"]):
                body_records = [r for r in claim.records(issue["body"]) if r["session"] == entry["session_id"]]
                source_records = [r for c in sources for r in claim.records(c.get("body") or "")]
                if not body_records or any(r not in source_records for r in body_records):
                    retained_status.append({**identity, "next_action": "reconcile the authored Current Status ownership"})
                    refused.append({**identity, "error": "status-only ownership has no matching canonical structured source"})
            for source in sources:
                source_identity = {**identity, "claim_comment": source["id"]}
                parsed = claim.records(source.get("body") or "")
                if not parsed:
                    index = comments.index(source)
                    if (claim.released_explanatory_note(source.get("body") or "", index, comments)
                            or claim.attested_handoff_note(source.get("body") or "", index, comments)):
                        continue
                    evidence = release.followup_release(source, comments, actor=actor,
                        session=entry["session_id"], ended=ended)
                    if evidence is not None and (source.get("user") or {}).get("login") == actor:
                        released.append({**source_identity,
                            "handoff_url": f"https://github.com/{repo}/issues/{number}#issuecomment-{evidence['id']}"})
                    else:
                        refused.append(source_refusal(source, "ownership record has no qualifying exact-ID release"))
                    continue
                if len(parsed) != 1 or parsed[0]["session"] != entry["session_id"]:
                    refused.append(source_refusal(source, "ownership record has no qualifying exact-ID release"))
                    continue
                if (source.get("user") or {}).get("login") != actor:
                    refused.append(source_refusal(source, "source claim belongs to another automation identity"))
                    continue
                evidence = None
                for candidate in comments:
                    if (claim.released_claim_id(candidate.get("body") or "") != source["id"]
                            or not release.exact_source_release(source["id"], candidate, comments)):
                        continue
                    receipt = release.receipt_for(candidate)
                    # Worker releases precede their end. A validated Supervisor
                    # receipt may attest a later end than this verified closure:
                    # its producer checked activity through that later time, and
                    # this finish has verified the source is still ended. A later
                    # receipt may also follow an earlier ineligible naked release;
                    # inspect each canonical pair.
                    if receipt is not None:
                        eligible = "_release_evidence" in candidate
                    else:
                        eligible = release.stamp(candidate.get("updated_at") or candidate.get("created_at")) <= ended
                    if not eligible or not release.related_sessions_match(candidate.get("body") or "", entry["session_id"]):
                        continue
                    evidence = candidate
                    break
                if evidence is None:
                    refused.append(source_refusal(source, "source claim has no valid exact-ID release for this ended source"))
                    continue
                released.append({**source_identity,
                    "handoff_url": f"https://github.com/{repo}/issues/{number}#issuecomment-{evidence['id']}"})
            # Use the same validated discussion, retaining unresolved follow-ups
            # and foreign ownership. A failed read is never an empty history.
            try:
                active = active_claims(reader, repo, number, entry["session_id"], actor, comments=comments, verified=True)
            except OwnershipDiagnostic as error:
                active = error.source_groups
                if not any(r.get("claim_comment") == error.claim_comment and r["repository"] == repo
                           and r["issue"] == number for r in refused):
                    source = next(c for c in comments if c["id"] == error.claim_comment)
                    refused.append(source_refusal(source, str(error)))
            for source, related, unresolved in active:
                for identifier in [source["id"], *related, *unresolved]:
                    if not any(r.get("claim_comment") == identifier and r["repository"] == repo
                               and r["issue"] == number for r in refused):
                        refused.append(source_refusal(next(c for c in comments if c["id"] == identifier), "ownership remains active"))
        except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError,
                GitHubReadError, GitHubReadShapeError) as error:
            refused.append({**identity, "error": str(error)})
    scope = entry["_finish_scope"]
    result_status = "needs reconciliation" if refused else "already released" if released else "nothing to release"
    if scope == "explicit scope":
        result_status += " in explicit scope"
    report = {"session_id": entry["session_id"], "ended_at": entry["ended_at"], "claims": [], "released_claims": released,
            "status": result_status, "coverage": scope,
            "handoff_url": released[-1]["handoff_url"] if released else None,
            "dry_run": not apply, "retained_status_claims": retained_status, "issue_refusals": refused,
            "pr_claim_handoffs": [], "closure_evidence": "matched PID/TTY log"}
    if refused or retained_status:
        report["next_action"] = ("Reconcile the reported records using "
            "https://github.com/cbusillo/codex-skills/blob/main/skills/supervisor/references/helpers.md#finish-claim-reconciliation "
            "and rerun finish.")
    return report


def verified_release_checkout(checkout_root, repo):
    checkout = checkout_root / repo.split("/")[1]
    remote = claim.run_read(["git", "remote", "get-url", "origin"], cwd=checkout).strip()
    if not re.search(rf"[:/]{re.escape(repo)}(?:\.git)?$", remote, re.IGNORECASE):
        raise ValueError("release checkout must match the canonical repository")
    return checkout


def plan_issue(reader, repo, number, entry, actor, handoff_prs: set[str], pulls_by_repo, checkout_root):
    plans, retained_status = [], []
    reader.deadline_at = time.time() + entry.get("_read_deadline_seconds", 600)
    issue = reader.get_json(f"/repos/{repo}/issues/{number}", step="release_canonical_issue")
    if "pull_request" in issue:
        sources = active_claims(reader, repo, number, entry["session_id"], actor)
        comments = reader.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="pr_inventory_source")
        active_ids = {identifier for source, related, unresolved in sources for identifier in [source["id"], *related, *unresolved]}
        for row in entry.get("_inventory_claims", []):
            if row["repo"] != repo or row["issue"] != number:
                continue
            expected = next((c for c in comments if c["id"] == row["claim_comment_id"]), None)
            if expected is None or (expected.get("user") or {}).get("login") != actor or not belongs_to_session(expected.get("body") or "", entry["session_id"]):
                raise ValueError("inventory PR source is missing or lacks exact native-session/author evidence")
            entry.setdefault("_inventory_results", {})[(repo, number, row["claim_comment_id"])] = {
                "repository": repo, "issue": number, "claim_comment": row["claim_comment_id"],
                "status": "manual_release_required" if row["claim_comment_id"] in active_ids else "already_released"}
        checkout = verified_release_checkout(checkout_root, repo) if sources else None
        rows = {}
        for source, related, unresolved in sources:
            if entry.get("_inventory") and not any(row["repo"] == repo and row["issue"] == number
                    and row["claim_comment_id"] == source["id"] for row in entry["_inventory_claims"]):
                raise ValueError("inventory PR source needs its structured claim selected explicitly")
            if unresolved:
                raise ValueError("PR ownership follow-ups need verified exact-ID releases")
            argv = ["uv", "run", str(GITHUB / "gh-plan.py"), "--repo", repo,
                    "release-claim", f"https://github.com/{repo}/pull/{number}",
                    "--claim-comment", str(source["id"]),
                    "--evidence-comment", "<verified-closed-session-attestation-url>",
                    "--role", "supervisor", "--session", entry.get("_releaser_session", "<supervisor-native-session>"),
                    "--confirm-session-ended"]
            for related_id in related:
                argv.extend(["--related-claim-comment", str(related_id)])
            row = {"repository": repo, "pull_request": number,
                   "claim_comments": [source["id"], *related],
                   "status": "manual_release_required", "release_argv": argv,
                   "dry_run_argv": [*argv, "--dry-run"], "checkout": str(checkout),
                   "attestation_body": closure_attestation(entry)}
            rows[(repo, number, source["id"])] = row
        entry.setdefault("_pr_claim_handoffs", {}).update(rows)
        return [], retained_status
    comments = reader.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="session_issue_filter")
    if not any(belongs_to_session(c.get("body") or "", entry["session_id"]) for c in comments) and not belongs_to_session(issue.get("body") or "", entry["session_id"]):
        if entry.get("_inventory"):
            raise ValueError("inventory source is missing or lacks exact native-session evidence")
        return [], retained_status
    if belongs_to_session(issue.get("body") or "", entry["session_id"]):
        retained_status.append({"repository": repo, "issue": number,
                                "next_action": "reconcile Current Status or use the supported --resume-from handoff route"})
    sources = active_claims(reader, repo, number, entry["session_id"], actor)
    active_ids = {identifier for source, related, unresolved in sources for identifier in [source["id"], *related, *unresolved]}
    for row in entry.get("_inventory_claims", []):
        if row["repo"] != repo or row["issue"] != number:
            continue
        expected = next((c for c in comments if c["id"] == row["claim_comment_id"]), None)
        if expected is None or (expected.get("user") or {}).get("login") != actor or not belongs_to_session(expected.get("body") or "", entry["session_id"]):
            raise ValueError("inventory source is missing or lacks exact native-session/author evidence")
        entry.setdefault("_inventory_results", {})[(repo, number, row["claim_comment_id"])] = {
            "repository": repo, "issue": number, "claim_comment": row["claim_comment_id"],
            "status": "active" if row["claim_comment_id"] in active_ids else "already_released"}
    for source, related, unresolved in sources:
        if entry.get("_inventory") and not any(r["repo"] == repo and r["issue"] == number
                and r["claim_comment_id"] == source["id"] for r in entry["_inventory_claims"]):
            continue
        record = release.source_record(source)
        if repo not in pulls_by_repo:
            pulls_by_repo[repo] = reader.paged_json(f"/repos/{repo}/pulls", params={"state": "open"}, step_prefix="retained_session_prs")
        pulls = list(pulls_by_repo[repo])
        mentioned: set[str] = handoff_prs | ({record["refresh_pr"]} if record.get("refresh_pr") else set())
        inventory_prs = {r["retained_pr_url"] for r in entry.get("_inventory_claims", [])
                         if r["repo"] == repo and r["issue"] == number and r.get("retained_pr_url")}
        if any(not re.fullmatch(rf"https://github\.com/{re.escape(repo)}/pull/([1-9]\d*)", url) for url in inventory_prs):
            raise ValueError("inventory retained PR must be an exact same-repository URL")
        mentioned.update(inventory_prs)
        for url in sorted(mentioned):
            match = re.fullmatch(rf"https://github\.com/{re.escape(repo)}/pull/([1-9]\d*)", url)
            if match and not any(p["html_url"] == url for p in pulls):
                pulls.append(reader.get_json(f"/repos/{repo}/pulls/{match[1]}", step="handoff_retained_pr"))
        prs = [p["html_url"] for p in pulls
               if p["html_url"] in inventory_prs or (p.get("head") or {}).get("ref") == record["branch"]
               or (p["html_url"] in mentioned and (p.get("user") or {}).get("login") == actor
                   and (p.get("state") == "open" or p.get("merged_at"))
                   and claim.pr_issue_reference(p, number, repo=repo, attestation=True))]
        plans.append((repo, number, source["id"], related, prs))
        if unresolved:
            raise ValueError(f"ownership follow-ups {unresolved} lack a verified source/session relationship; obtain authored exact-ID releases")
    if not plans and retained_status and not any(claim.records(c.get("body") or "") for c in comments):
        raise ValueError("status-only ownership has no structured source; reconcile its authored ownership record")
    covered = {identifier for _, _, source_id, related, _ in plans for identifier in [source_id, *related]}
    if any(r["repo"] == repo and r["issue"] == number and r["claim_comment_id"] in active_ids
           and r["claim_comment_id"] not in covered for r in entry.get("_inventory_claims", [])):
        raise ValueError("active inventory follow-up has no selected structured source; obtain its authored exact-ID release")
    if plans:
        verified_release_checkout(checkout_root, repo)
    return plans, retained_status


def preview_guards(entry, *, reader, repo, number, source_id, related, prs, body, actor, supervisor_session, checkout_root):
    comments = reader.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="preview_source_activity")
    source = next(c for c in comments if c["id"] == source_id)
    related_comments = [c for c in comments if c["id"] in related]
    if len(related_comments) != len(related):
        raise ValueError("related ownership changed during preview")
    local = claim.local_inventory(repo, number, cwd=checkout_root / repo.split("/")[1])
    now = datetime.now(timezone.utc).isoformat()
    prospective = {"body": body, "user": {"login": actor}, "created_at": now, "updated_at": now}
    branches = set()
    issue_refusal = None
    for url in prs:
        match = re.fullmatch(rf"https://github\.com/{re.escape(repo)}/pull/([1-9]\d*)", url)
        if not match:
            raise ValueError("retained PR is not a same-repository canonical URL")
        pull = reader.get_json(f"/repos/{repo}/pulls/{match[1]}", step="preview_retained_pr")
        commit = reader.get_json(f"/repos/{repo}/commits/{pull['head']['sha']}", step="preview_retained_head")
        try:
            release.check_retained_pr(pull, commit, repo=repo, number=number, actor=actor, ended=entry["ended_at"])
        except release.IssueReleaseRefusal as error:
            issue_refusal = error
        branches.add(pull["head"]["ref"])
    # This prospective attestation is never posted or returned as release evidence.
    release.prepare_release(source, prospective, comments, local, actor=actor,
        role="supervisor", releaser_session=supervisor_session, evidence_url="unposted preview",
        retained_prs=prs, related=related_comments, checked_prs=prs, retained_branches=branches)
    if issue_refusal is not None:
        raise issue_refusal
    return {"status": "passed_read_only", "attestation_posted": False,
            "requires_command_preflight_after_posting": True, "session_coverage": local.get("session_coverage")}


def closure_attestation(entry):
    body = (f"Closed session {entry['session_id']}\nEnded at: {entry['ended_at']}\nSafe to exit: yes\n\n"
            f"Handoff context ({entry.get('_handoff_provenance', 'Supervisor transcript-verified ledger')}): {entry['handoff_url']}\n")
    if entry.get("_inventory"):
        body += "Closure evidence: Supervisor verified inventory; original PID/TTY unavailable. The caller confirms actual session inactivity, including unavailable native peers.\n"
    return body


def apply_plans(entry, supervisor_session, plans, status, checkout_root, actor, apply, report, reader):
    if apply:
        # Session-wide refusals must be known before the first claim is released.
        preflight = {"claims": [], "issue_refusals": []}
        apply_plans(entry, supervisor_session, plans, status, checkout_root, actor, False, preflight, reader)
        refused = {(item["repository"], item["issue"], item["claim_comment"])
                   for item in preflight["claims"] if item["release_preflight"].get("status") == "refused"}
        report["issue_refusals"].extend(preflight["issue_refusals"])
        report["claims"].extend(item for item in preflight["claims"]
                                if (item["repository"], item["issue"], item["claim_comment"]) in refused)
        plans = [plan for plan in plans if plan[:3] not in refused]
    for repo, number, source_id, related, prs in plans:
        if validate_closure(entry, supervisor_session) != status:
            raise ValueError("source transcript changed; preserve remaining claims")
        body = closure_attestation(entry)
        item = {"repository": repo, "issue": number, "claim_comment": source_id,
                "related_claim_comments": related,
                "retained_prs": prs, "attestation_body": body,
                "release_preflight": "requires a posted attestation; apply previews release-claim before releasing"}
        report["claims"].append(item)
        if not apply:
            try:
                item["release_preflight"] = preview_guards(entry, reader=reader, repo=repo,
                    number=number, source_id=source_id, related=related, prs=prs, body=body,
                    actor=actor, supervisor_session=supervisor_session, checkout_root=checkout_root)
            except release.IssueReleaseRefusal as error:
                item["release_preflight"] = {"status": "refused", "error": str(error)}
                report["issue_refusals"].append({"repository": repo, "issue": number, "error": str(error)})
            except (ValueError, KeyError, TypeError, StopIteration, OSError, GitHubReadError, GitHubReadShapeError) as error:
                item["release_preflight"] = {"status": "refused", "error": str(error)}
                raise ValueError(str(error)) from error
        if apply:
            result = comment("issue", number, body, repo=repo, expected_actor=actor,
                             operation="github.comment.issue", dedupe_body=True)
            url = result["comment"]["url"]
            item["evidence_url"] = url
            if validate_closure(entry, supervisor_session) != status:
                raise ValueError("source transcript changed after attestation; preserve claims")
            try:
                preview = run_release(repo, number, source_id, related, prs, url, supervisor_session, checkout_root, True)
            except ConfirmedIssueRefusal as error:
                if validate_closure(entry, supervisor_session) != status:
                    raise ValueError("source activity changed after issue refusal; preserve remaining claims") from error
                item["release_refusal"] = error.payload
                report["issue_refusals"].append({"repository": repo, "issue": number, "error": str(error)})
                continue
            item["release_body"] = preview["release_body"]
            if validate_closure(entry, supervisor_session) != status:
                raise ValueError("source transcript changed after preview; preserve claims")
            item["release"] = run_release(repo, number, source_id, related, prs, url, supervisor_session, checkout_root, False)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("finish", "backfill"))
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--closed-log", type=Path,
                        help="shared host-local closure log; an explicit private log is not successor evidence")
    parser.add_argument("--session-id")
    parser.add_argument("--supervisor-session", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", action="append", help="scope disposable trials; omission discovers all owner repositories")
    parser.add_argument("--checkout-root", type=Path, help="existing per-repository checkouts; required only for backfill")
    parser.add_argument("--confirm-session-ended", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--deadline-seconds", type=int, default=600)
    parser.add_argument("--account-config", type=Path)
    parser.add_argument("--transcript-root", type=Path, action="append", default=[])
    args = parser.parse_args()
    reports = []
    lock_stream = None
    try:
        if args.mode == "backfill" and args.checkout_root is None:
            raise ValueError("backfill requires --checkout-root for canonical release preflight")
        if args.closed_log is None:
            if args.mode != "finish":
                raise ValueError("backfill requires an explicit --closed-log")
            args.closed_log = closure.closed_log()
        if args.apply:
            args.closed_log.parent.mkdir(parents=True, exist_ok=True)
            lock_stream = os.fdopen(os.open(str(args.closed_log) + ".release.lock", os.O_CREAT | os.O_RDWR, 0o600), "w")
            try:
                fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("another closure/release operation owns this log") from error
        if not args.confirm_session_ended:
            raise ValueError("Supervisor must verify actual process exit")
        if args.deadline_seconds <= 0:
            raise ValueError("discovery deadline must be positive")
        actor = github_identity.automation_login()
        if not actor:
            raise ValueError("requires the configured automation identity")
        if args.mode == "finish":
            if not args.ledger or not args.session_id:
                raise ValueError("finish requires --ledger and one --session-id")
            entries = [{k: v for k, v in e.items() if not k.startswith("_")}
                       for e in session_record.load_ledger(args.ledger) if e["session_id"] == args.session_id]
            if len(entries) != 1:
                raise ValueError("native session must match exactly one ledger entry")
            entries[0]["_transcript_roots"] = transcript_roots(entries[0]["harness"], args.account_config, args.transcript_root)
            # record_closure verifies exit against the saved closure on retry,
            # or a fresh observation time for a new closure. A ledger timestamp
            # alone is not proof that this generation has already ended.
            if args.apply:
                entries = [record_closure(args.closed_log, entries[0], args.supervisor_session)]
            else:
                entries = [record_closure(args.closed_log, entries[0], args.supervisor_session, persist=False)]
            entries[0]["_finish"] = True
            entries[0]["_finish_scope"] = "explicit scope" if args.repo else "accessible repositories under the requested owner"
        else:
            rows = json.loads(args.closed_log.read_text())
            if isinstance(rows, list) and rows and isinstance(rows[0], dict) and "safe_exit_yes" in rows[0]:
                roots = {h: transcript_roots(h, args.account_config, args.transcript_root) for h in ("codex", "claude")}
                entries, inventory_reports = backfill_inventory.load(rows, roots)
                reports.extend(inventory_reports)
                owned = []
                for entry in entries:
                    if any(r["repo"].split("/")[0].casefold() != args.owner.casefold() for r in entry["_inventory_claims"]):
                        reports.append({"session_id": entry["session_id"], "error": "inventory group includes a repository outside the requested owner", "released": False})
                    else:
                        owned.append(entry)
                entries = owned
            else:
                private = args.closed_log.resolve() != closure.closed_log().resolve()
                entries = [{**{k: v for k, v in saved.items() if not k.startswith("_")},
                            **({"_closure_source": {"path": str(args.closed_log.resolve()), "source": saved}} if private else {})}
                           for saved in session_record.load_ledger(args.closed_log, closed_log=True)]
            entries = [{**e, "_backfill": True} for e in entries]
            if args.session_id:
                entries = [e for e in entries if e["session_id"] == args.session_id]
            if not entries:
                raise ValueError("closed-session log has no matching records")
        entries = [{**e,
                    "_transcript_roots": e.get("_transcript_roots") or transcript_roots(e["harness"], args.account_config, args.transcript_root),
                    "_read_deadline_seconds": args.deadline_seconds} for e in entries]
        verified = []
        for entry in entries:
            try:
                if not entry.get("_finish"):
                    validate_closure(entry, args.supervisor_session)
                verified.append(entry)
            except (ValueError, KeyError, TypeError, OSError) as error:
                inventory_rows: list[dict[str, Any]] = entry.get("_inventory_claims", [])
                reports.append({"session_id": entry["session_id"], "error": str(error), "released": False,
                                "inventory_claims": [{"repository": r["repo"], "issue": r["issue"],
                                    "claim_comment": r["claim_comment_id"], "status": "preserved"}
                                    for r in inventory_rows]})
        entries = verified
        reader = GitHubReader(gh_prefix_args=automation_only_gh_prefix_args(), deadline_at=time.time() + args.deadline_seconds)
        if not entries:
            raise ValueError("no verified closed sessions; inspect the per-session reports")
        if all(e.get("_inventory") for e in entries):
            for entry in entries:
                for row in entry["_inventory_claims"]:
                    if args.repo and row["repo"] not in args.repo:
                        reports.append({"session_id": entry["session_id"], "repository": row["repo"], "issue": row["issue"],
                            "claim_comment": row["claim_comment_id"], "status": "out_of_scope", "released": False,
                            "error": "preserved outside explicit --repo scope"})
            issues = sorted({(r["repo"], r["issue"]) for e in entries for r in e["_inventory_claims"]
                             if not args.repo or r["repo"] in args.repo})
            repos = sorted({r for r, _ in issues})
        else:
            issues, repos = discover(reader, args.owner, entries, args.repo)
        for entry in entries:
            try:
                reports.append(release_session(reader, entry, actor, args.supervisor_session, issues,
                                                args.checkout_root, args.apply))
            except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError,
                    GitHubReadError, GitHubReadShapeError, CommentError) as error:
                report = error.report if isinstance(error, PartialRelease) else {"session_id": entry["session_id"]}
                reports.append({**report, "error": str(error), "next_action": "inspect records before retry"})
        ok = not any("error" in r or r.get("issue_refusals") or r.get("pr_claim_handoffs") for r in reports)
        print(json.dumps({"ok": ok, "reports": reports, "repositories_checked": repos,
                          "needs_reconciliation": any(r.get("retained_status_claims") or r.get("issue_refusals") or r.get("pr_claim_handoffs") or "error" in r for r in reports),
                          "coverage": "exact verified inventory claims" if all(e.get("_inventory") for e in entries) else "explicit scope" if args.repo else "accessible repositories under the requested owner",
                          "outside_scope": "other repository owners and other automation App identities; use their configured evidence-backed release-claim route"}))
        return 0 if ok else 1
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError,
            GitHubReadError, GitHubReadShapeError, CommentError) as error:
        if isinstance(error, PartialRelease):
            reports.append(error.report)
        print(json.dumps({"ok": False, "error": str(error), "completed_sessions": reports,
                          "next_action": "inspect posted records and remaining claims; do not replay an uncertain write"}))
        return 1
    finally:
        if lock_stream is not None:
            lock_stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
