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
import account_choice
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


class ConfirmedIssueRefusal(ValueError):
    def __init__(self, payload):
        super().__init__(payload.get("error", "confirmed issue release refusal"))
        self.payload = payload


def verify_closed(entry, supervisor_session):
    if entry.get("supervisor_owned") is not True or entry["session_id"] == supervisor_session:
        raise ValueError("requires another explicitly Supervisor-owned session")
    pid, tty = entry.get("pid"), entry.get("tty")
    if type(pid) is not int or pid <= 0 or not isinstance(tty, str) or not tty:
        raise ValueError("closure requires the matched process PID and TTY")
    rows = inventory()
    if any(p == pid or (not entry.get("_backfill") and t == tty and not idle_terminal_command(c)) for p, t, c in rows):
        raise ValueError("source PID (possibly reused) or work on its TTY remains; preserve claims and verify native identity before the documented manual release-claim route")
    for _, _, command in rows:
        if not command.split() or Path(command.split()[0]).name not in {"codex", "claude"}:
            continue
        # ps does not preserve shell quoting. Inspect only native resume
        # operands; arbitrary quoted prompt text is neither syntax nor identity.
        argv = command.split()
        if any(arg in {"resume", "--resume", "-r"} and index + 1 < len(argv)
               and argv[index + 1].strip("\"'") == entry["session_id"]
               or arg == f"--resume={entry['session_id']}" for index, arg in enumerate(argv)):
            raise ValueError("source native session is visible in a resumed harness process")
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


def record_closure(path, entry, supervisor_session, *, persist=True):
    """Observe exit now; never replace an older actual closure time on retry."""
    records = session_record.load_ledger(path) if path.exists() else []
    existing = [r for r in records if r["session_id"] == entry["session_id"]]
    if existing:
        if any(existing[0].get(k) != entry.get(k) for k in (
            "pid", "tty", "harness", "handoff_url", "supervisor_owned"
        )) or Path(existing[0]["transcript"]).resolve() != Path(entry["transcript"]).resolve():
            raise ValueError("closure log identity changed; preserve the original record")
        closed = {**existing[0], **{k: v for k, v in entry.items() if k.startswith("_")}}
        validate_closure(closed, supervisor_session)
        return closed
    verify_closed(entry, supervisor_session)
    closed = {**entry, "ended_at": datetime.now(timezone.utc).isoformat(),
              "process_exit_verified": True}
    validate_closure(closed, supervisor_session)
    if not persist:
        return closed
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump([*records, {k: v for k, v in closed.items() if not k.startswith("_")}], stream)
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
        except (ValueError, GitHubReadError) as error:
            if isinstance(error, GitHubReadError) and getattr(error.result, "status", None) not in {403, 404}:
                raise
            comments = [release.without_release(r) if r["id"] == c["id"] else r for r in comments]
    conflicts, _ = claim.discussion_evidence("", comments, {
        "session": "", "worker": "", "branch": "", "claimed_at": ""
    })
    active_ids = {c["id"] for c in conflicts if c.get("source") == "comment"}
    sources = []
    for c in comments:
        records = claim.records(c.get("body") or "")
        if (c["id"] in active_ids and not records
                and belongs_to_session(c.get("body") or "", session)
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
            related = [r["id"] for r in comments[comments.index(c) + 1:] if r["id"] in active_ids
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
        if dry_run and payload.get("release_refusal_scope") == release.ISSUE_REFUSAL_SCOPE and "post_release" not in payload.get("completed_steps", []):
            raise ConfirmedIssueRefusal(payload)
        raise ValueError(f"release-claim did not confirm success; preserve records: {json.dumps(payload)}")
    return payload


def release_session(reader, entry, actor, supervisor_session, issues, checkout_root, apply):
    status = validate_closure(entry, supervisor_session)
    handoff_record = handoff(reader, entry, actor)
    plans = []
    retained_status = []
    pulls_by_repo = {}
    handoff_prs = set(re.findall(r"https://github\.com/[^/\s]+/[^/\s]+/pull/[1-9]\d*", handoff_record.get("body") or ""))
    refused = []
    for repo, number in issues:
        try:
            issue_plans, status_claims = plan_issue(reader, repo, number, entry, actor, handoff_prs, pulls_by_repo, checkout_root, apply)
            plans.extend(issue_plans)
            retained_status.extend(status_claims)
        except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError, GitHubReadError, GitHubReadShapeError) as error:
            refused.append({"repository": repo, "issue": number, "error": str(error)})
    report = {"session_id": entry["session_id"], "claims": [], "dry_run": not apply,
              "retained_status_claims": retained_status, "issue_refusals": refused}
    try:
        return apply_plans(entry, supervisor_session, plans, status, checkout_root, actor, apply, report)
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError, CommentError) as error:
        raise PartialRelease(error, report) from error


def plan_issue(reader, repo, number, entry, actor, handoff_prs, pulls_by_repo, checkout_root, apply):
    plans, retained_status = [], []
    reader.deadline_at = time.time() + entry.get("_read_deadline_seconds", 600)
    issue = reader.get_json(f"/repos/{repo}/issues/{number}", step="release_canonical_issue")
    if "pull_request" in issue:
        return [], retained_status
    comments = reader.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="session_issue_filter")
    if not any(belongs_to_session(c.get("body") or "", entry["session_id"]) for c in comments) and not belongs_to_session(issue.get("body") or "", entry["session_id"]):
        return [], retained_status
    if belongs_to_session(issue.get("body") or "", entry["session_id"]):
        retained_status.append({"repository": repo, "issue": number,
                                "next_action": "reconcile Current Status or use the supported --resume-from handoff route"})
    for source, related, unresolved in active_claims(reader, repo, number, entry["session_id"], actor):
        record = release.source_record(source)
        if repo not in pulls_by_repo:
            pulls_by_repo[repo] = reader.paged_json(f"/repos/{repo}/pulls", params={"state": "open"}, step_prefix="retained_session_prs")
        pulls = list(pulls_by_repo[repo])
        mentioned = handoff_prs | ({record["refresh_pr"]} if record.get("refresh_pr") else set())
        for url in sorted(mentioned):
            match = re.fullmatch(rf"https://github\.com/{re.escape(repo)}/pull/([1-9]\d*)", url)
            if match and not any(p["html_url"] == url for p in pulls):
                pulls.append(reader.get_json(f"/repos/{repo}/pulls/{match[1]}", step="handoff_retained_pr"))
        prs = [p["html_url"] for p in pulls
               if (p.get("head") or {}).get("ref") == record["branch"]
               or (p["html_url"] in mentioned and (p.get("user") or {}).get("login") == actor
                   and (p.get("state") == "open" or p.get("merged_at"))
                   and claim.pr_issue_reference(p, number, repo=repo, attestation=True))]
        plans.append((repo, number, source["id"], related, prs))
        if unresolved:
            raise ValueError(f"ownership follow-ups {unresolved} lack a verified source/session relationship; obtain authored exact-ID releases")
    if not plans and retained_status and not any(claim.records(c.get("body") or "") for c in comments):
        raise ValueError("status-only ownership has no structured source; reconcile its authored ownership record")
    if apply and plans:
        remote = claim.run_read(["git", "remote", "get-url", "origin"], cwd=checkout_root / repo.split("/")[1]).strip()
        if not re.search(rf"[:/]{re.escape(repo)}(?:\.git)?$", remote, re.IGNORECASE):
            raise ValueError("release checkout must match the canonical repository")
    return plans, retained_status


def apply_plans(entry, supervisor_session, plans, status, checkout_root, actor, apply, report):
    for repo, number, source_id, related, prs in plans:
        if validate_closure(entry, supervisor_session) != status:
            raise ValueError("source transcript changed; preserve remaining claims")
        body = (f"Closed session {entry['session_id']}\nEnded at: {entry['ended_at']}\nSafe to exit: yes\n\n"
                f"Transcript-verified handoff: {entry['handoff_url']}\n")
        item = {"repository": repo, "issue": number, "claim_comment": source_id,
                "retained_prs": prs, "attestation_body": body,
                "release_preflight": "requires a posted attestation; apply previews release-claim before releasing"}
        report["claims"].append(item)
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
    parser.add_argument("--closed-log", type=Path, required=True)
    parser.add_argument("--session-id")
    parser.add_argument("--supervisor-session", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", action="append", help="scope disposable trials; omission discovers all owner repositories")
    parser.add_argument("--checkout-root", type=Path, required=True)
    parser.add_argument("--confirm-session-ended", action="store_true")
    parser.add_argument("--verified-handoff", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--deadline-seconds", type=int, default=600)
    parser.add_argument("--account-config", type=Path)
    parser.add_argument("--transcript-root", type=Path, action="append", default=[])
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
        if args.deadline_seconds <= 0:
            raise ValueError("discovery deadline must be positive")
        actor = github_identity.automation_login()
        if not actor:
            raise ValueError("requires the configured automation identity")
        if args.mode == "finish":
            if not args.ledger or not args.session_id:
                raise ValueError("finish requires --ledger and one --session-id")
            entries = [e for e in session_record.load_ledger(args.ledger) if e["session_id"] == args.session_id]
            if len(entries) != 1:
                raise ValueError("native session must match exactly one ledger entry")
            entries[0]["_transcript_roots"] = transcript_roots(entries[0]["harness"], args.account_config, args.transcript_root)
            verify_closed(entries[0], args.supervisor_session)
            if args.apply:
                entries = [record_closure(args.closed_log, entries[0], args.supervisor_session)]
            else:
                entries = [record_closure(args.closed_log, entries[0], args.supervisor_session, persist=False)]
        else:
            entries = session_record.load_ledger(args.closed_log)
            entries = [{**e, "_backfill": True} for e in entries]
            if args.session_id:
                entries = [e for e in entries if e["session_id"] == args.session_id]
            if not entries:
                raise ValueError("closed-session log has no matching records")
        entries = [{**e, "_transcript_roots": transcript_roots(e["harness"], args.account_config, args.transcript_root),
                    "_read_deadline_seconds": args.deadline_seconds} for e in entries]
        verified = []
        for entry in entries:
            try:
                validate_closure(entry, args.supervisor_session)
                verified.append(entry)
            except (ValueError, KeyError, TypeError, OSError) as error:
                reports.append({"session_id": entry["session_id"], "error": str(error), "released": False})
        entries = verified
        reader = GitHubReader(gh_prefix_args=automation_only_gh_prefix_args(), deadline_at=time.time() + args.deadline_seconds)
        if not entries:
            raise ValueError("no verified closed sessions; inspect the per-session reports")
        issues, repos = discover(reader, args.owner, entries, args.repo)
        for entry in entries:
            try:
                reports.append(release_session(reader, entry, actor, args.supervisor_session, issues,
                                                args.checkout_root, args.apply))
            except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError,
                    GitHubReadError, GitHubReadShapeError, CommentError) as error:
                report = error.report if isinstance(error, PartialRelease) else {"session_id": entry["session_id"]}
                reports.append({**report, "error": str(error), "next_action": "inspect records before retry"})
        ok = not any("error" in r or r.get("issue_refusals") for r in reports)
        print(json.dumps({"ok": ok, "reports": reports, "repositories_checked": repos,
                          "needs_reconciliation": any(r.get("retained_status_claims") or r.get("issue_refusals") or "error" in r for r in reports),
                          "coverage": "explicit scope" if args.repo else "accessible repositories under the requested owner",
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
