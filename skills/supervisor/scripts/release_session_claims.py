#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Record verified session closure and release its automation claims. Dry run by default."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import fcntl
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

import session_record
from close_ttys import idle_terminal_command, inventory

GITHUB = Path(__file__).resolve().parents[2] / "github/scripts"
sys.path.insert(0, str(GITHUB))
import github_identity
import github_plan_claim as claim
import github_plan_release as release
from github_read import GitHubReader, GitHubReadError, GitHubReadShapeError, automation_only_gh_prefix_args
from github_comment import CommentError, comment

REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
HANDOFF = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/issues/([1-9]\d*)#issuecomment-([1-9]\d*)")


class PartialRelease(ValueError):
    def __init__(self, error, report):
        super().__init__(str(error))
        self.report = report


def verify_closed(entry, supervisor_session):
    if entry.get("supervisor_owned") is not True or entry["session_id"] == supervisor_session:
        raise ValueError("requires another explicitly Supervisor-owned session")
    pid, tty = entry.get("pid"), entry.get("tty")
    if type(pid) is not int or pid <= 0 or not isinstance(tty, str) or not tty:
        raise ValueError("closure requires the matched process PID and TTY")
    rows = inventory()
    if any(p == pid or t == tty and not idle_terminal_command(c) for p, t, c in rows):
        raise ValueError("source PID or work on its TTY remains; preserve its claims")
    status = session_record.session_status(entry)
    if not status["safe_verdict"]:
        raise ValueError("latest transcript lacks an unqualified Safe to exit: yes")
    return status


def validate_closure(entry, supervisor_session):
    if entry.get("process_exit_verified") is not True:
        raise ValueError("closed-session log lacks verified process exit")
    ended = release.stamp(entry["ended_at"])
    if ended > datetime.now(timezone.utc):
        raise ValueError("closure timestamp is in the future")
    status = verify_closed(entry, supervisor_session)
    if not status["timestamp"] or release.stamp(status["timestamp"]) > ended:
        raise ValueError("transcript resumed after the recorded closure")
    return status


def record_closure(path, entry, supervisor_session):
    """Observe exit now; never replace an older actual closure time on retry."""
    records = session_record.load_ledger(path) if path.exists() else []
    existing = [r for r in records if r["session_id"] == entry["session_id"]]
    if existing:
        if any(existing[0].get(k) != entry.get(k) for k in (
            "pid", "tty", "harness", "handoff_url", "supervisor_owned"
        )) or Path(existing[0]["transcript"]).resolve() != Path(entry["transcript"]).resolve():
            raise ValueError("closure log identity changed; preserve the original record")
        validate_closure(existing[0], supervisor_session)
        return existing[0]
    verify_closed(entry, supervisor_session)
    closed = {**entry, "ended_at": datetime.now(timezone.utc).isoformat(),
              "process_exit_verified": True}
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump([*records, closed], stream)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return closed


def scan_start(entries):
    timestamps = [release.stamp(r["timestamp"]) for e in entries
                  for r in session_record.read_jsonl(Path(e["transcript"])) if r.get("timestamp")]
    if not timestamps:
        raise ValueError("transcripts have no timestamped history for discovery")
    return (min(timestamps) - timedelta(seconds=1)).isoformat()


def discover(reader, owner, entries, repos=None):
    """Complete paged comment inventory, including closed issues, since session start."""
    if repos is None:
        repositories = reader.paged_json("/installation/repositories", collection_key="repositories",
                                         step_prefix="claim_repositories")
        repos = [r["full_name"] for r in repositories
                 if r["full_name"].split("/")[0].casefold() == owner.casefold()]
    if not all(REPO.fullmatch(r) and r.split("/")[0].casefold() == owner.casefold() for r in repos):
        raise ValueError("repository scope must belong to the configured owner")
    sessions = {e["session_id"] for e in entries}
    since = scan_start(entries)
    issues = set()
    for repo in repos:
        comments = reader.paged_json(f"/repos/{repo}/issues/comments", step_prefix="session_claim_inventory",
                                     params={"since": since}, required_query_params={"since": since})
        for c in comments:
            body = c.get("body") or ""
            structured = any(r["session"] in sessions for r in claim.records(body))
            legacy = claim.has_ownership_assertion(body) and any(
                re.search(rf"(?m)^Session: {re.escape(s)}[ \t]*$", body) for s in sessions)
            if structured or legacy:
                match = re.fullmatch(rf"https://api\.github\.com/repos/{re.escape(repo)}/issues/([1-9]\d*)", c["issue_url"])
                if not match:
                    raise ValueError("claim comment has an unexpected canonical issue URL")
                issues.add((repo, int(match[1])))
    return sorted(issues), repos


def active_claims(reader, repo, number, session, actor):
    comments = reader.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="session_claim_discussion")
    # Use the same evidence validity and ownership classifier as claim, rather
    # than deciding that any release-shaped text erases ownership.
    for c, receipt in release.receipts(comments):
        match = HANDOFF.fullmatch(receipt["evidence_url"])
        try:
            if not match:
                raise ValueError("invalid evidence URL")
            evidence = reader.get_json(f"/repos/{match[1]}/issues/comments/{match[3]}", step="release_evidence")
            if evidence.get("issue_url") != f"https://api.github.com/repos/{match[1]}/issues/{match[2]}":
                raise ValueError("evidence belongs to another issue")
            release.validate_evidence(receipt, evidence, (c.get("user") or {}).get("login", ""))
        except ValueError:
            comments = [release.without_release(r) if r["id"] == c["id"] else r for r in comments]
    conflicts, _ = claim.discussion_evidence("", comments, {
        "session": "", "worker": "", "branch": "", "claimed_at": ""
    })
    active_ids = {c["id"] for c in conflicts if c.get("source") == "comment"}
    sources = []
    for c in comments:
        records = claim.records(c.get("body") or "")
        if (c["id"] in active_ids and not records
                and re.search(rf"(?m)^Session: {re.escape(session)}[ \t]*$", c.get("body") or "")
                and not any(r["session"] == session and re.match(
                    rf"Claimed by {re.escape(r['worker'])}(?:\s|$)", c.get("body") or "")
                    for source in comments for r in claim.records(source.get("body") or ""))):
            # The existing release route requires a structured source. Never
            # invent one or release an older hand-written source by worker alias.
            raise ValueError(f"hand-written claim {c['id']} needs its authored exact-ID release")
        if c["id"] in active_ids and any(r["session"] == session for r in records):
            if (c.get("user") or {}).get("login") != actor:
                raise ValueError("session has a claim held by another identity; report without releasing")
            record = release.source_record(c)
            related = [r["id"] for r in comments if r["id"] in active_ids
                       and not claim.records(r.get("body") or "")
                       and re.match(rf"Claimed by {re.escape(record['worker'])}(?:\s|$)", r.get("body") or "")
                       and (r.get("user") or {}).get("login") == actor]
            sources.append((c, related))
    return sources


def handoff(reader, entry, actor):
    match = HANDOFF.fullmatch(entry.get("handoff_url") or "")
    if not match:
        raise ValueError("requires an exact transcript-verified issue handoff URL")
    evidence = reader.get_json(f"/repos/{match[1]}/issues/comments/{match[3]}", step="session_handoff")
    if ((evidence.get("user") or {}).get("login") != actor
            or evidence.get("issue_url") != f"https://api.github.com/repos/{match[1]}/issues/{match[2]}"):
        raise ValueError("handoff identity or canonical issue differs")
    return evidence


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
    if result.returncode or payload.get("ok") is not True or payload.get("outcome_certainty") != "confirmed":
        raise ValueError(f"release-claim did not confirm success; preserve records: {json.dumps(payload)}")
    return payload


def release_session(reader, entry, actor, supervisor_session, issues, checkout_root, apply):
    status = validate_closure(entry, supervisor_session)
    handoff_record = handoff(reader, entry, actor)
    plans = []
    for repo, number in issues:
        for source, related in active_claims(reader, repo, number, entry["session_id"], actor):
            record = release.source_record(source)
            pulls = reader.paged_json(f"/repos/{repo}/pulls", params={"state": "open"}, step_prefix="retained_session_prs")
            prs = [p["html_url"] for p in pulls if (p.get("head") or {}).get("ref") == record["branch"]
                   or (p["html_url"] == record.get("refresh_pr")
                       or p["html_url"] in (handoff_record.get("body") or ""))
                   and claim.pr_issue_reference(p, number, repo=repo, attestation=True)]
            plans.append((repo, number, source["id"], related, prs))
    report = {"session_id": entry["session_id"], "claims": [], "dry_run": not apply}
    for repo, _, _, _, _ in plans:
        checkout = checkout_root / repo.split("/")[1]
        if apply:
            remote = claim.run_read(["git", "remote", "get-url", "origin"], cwd=checkout).strip()
            if not re.search(rf"[:/]{re.escape(repo)}(?:\.git)?$", remote, re.IGNORECASE):
                raise ValueError("release checkout must match the canonical repository")
    try:
        return apply_plans(entry, supervisor_session, plans, status, checkout_root, actor, apply, report)
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError, CommentError) as error:
        raise PartialRelease(error, report) from error


def apply_plans(entry, supervisor_session, plans, status, checkout_root, actor, apply, report):
    for repo, number, source_id, related, prs in plans:
        if validate_closure(entry, supervisor_session) != status:
            raise ValueError("source transcript changed; preserve remaining claims")
        body = (f"Closed session {entry['session_id']}\nEnded at: {entry['ended_at']}\nSafe to exit: yes\n\n"
                f"Transcript-verified handoff: {entry['handoff_url']}\n")
        item = {"repository": repo, "issue": number, "claim_comment": source_id,
                "retained_prs": prs, "attestation_body": body}
        report["claims"].append(item)
        if apply:
            result = comment("issue", number, body, repo=repo, expected_actor=actor,
                             operation="github.comment.issue", dedupe_body=True)
            url = result["comment"]["url"]
            item["evidence_url"] = url
            if validate_closure(entry, supervisor_session) != status:
                raise ValueError("source transcript changed after attestation; preserve claims")
            preview = run_release(repo, number, source_id, related, prs, url, supervisor_session, checkout_root, True)
            item["release_body"] = preview["release_body"]
            if validate_closure(entry, supervisor_session) != status:
                raise ValueError("source transcript changed after preview; preserve claims")
            item["release"] = run_release(repo, number, source_id, related, prs, url, supervisor_session, checkout_root, False)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("finish", "backfill"))
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--closed-log", type=Path, required=True)
    parser.add_argument("--session-id")
    parser.add_argument("--supervisor-session", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", action="append", help="scope disposable trials; omission discovers all owner repositories")
    parser.add_argument("--checkout-root", type=Path, required=True)
    parser.add_argument("--confirm-session-ended", action="store_true")
    parser.add_argument("--verified-handoff", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    reports = []
    lock_stream = None
    try:
        if args.apply:
            args.closed_log.parent.mkdir(parents=True, exist_ok=True)
            lock_stream = os.fdopen(os.open(str(args.closed_log) + ".lock", os.O_CREAT | os.O_RDWR, 0o600), "w")
            try:
                fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("another closure/release operation owns this log") from error
        if not args.confirm_session_ended or not args.verified_handoff:
            raise ValueError("Supervisor must verify actual process exit and transcript/issue handoff")
        actor = github_identity.automation_login()
        if not actor:
            raise ValueError("requires the configured automation identity")
        if args.mode == "finish":
            if not args.ledger or not args.session_id:
                raise ValueError("finish requires --ledger and one --session-id")
            entries = [e for e in session_record.load_ledger(args.ledger) if e["session_id"] == args.session_id]
            if len(entries) != 1:
                raise ValueError("native session must match exactly one ledger entry")
            verify_closed(entries[0], args.supervisor_session)
            if args.apply:
                entries = [record_closure(args.closed_log, entries[0], args.supervisor_session)]
            else:
                entries = [{**entries[0], "process_exit_verified": True,
                            "ended_at": datetime.now(timezone.utc).isoformat()}]
        else:
            entries = session_record.load_ledger(args.closed_log)
            if args.session_id:
                entries = [e for e in entries if e["session_id"] == args.session_id]
            if not entries:
                raise ValueError("closed-session log has no matching records")
        for entry in entries:
            validate_closure(entry, args.supervisor_session)
        reader = GitHubReader(gh_prefix_args=automation_only_gh_prefix_args(), deadline_at=time.time() + 180)
        issues, repos = discover(reader, args.owner, entries, args.repo)
        for entry in entries:
            reports.append(release_session(reader, entry, actor, args.supervisor_session, issues,
                                            args.checkout_root, args.apply))
        print(json.dumps({"ok": True, "reports": reports, "repositories_checked": repos,
                          "coverage": "explicit scope" if args.repo else "accessible owner repositories"}))
        return 0
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
