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
import backfill_inventory
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
    historical = entry.get("_inventory") is True
    if entry["session_id"] == supervisor_session or (
        not historical and entry.get("supervisor_owned") is not True
    ):
        raise ValueError("requires another explicitly Supervisor-owned session")
    if historical and (entry.get("safe_exit_yes") is not True or entry.get("still_running") is not False):
        raise ValueError("inventory closure requires the Supervisor's verified safe exit and inactivity")
    pid, tty = entry.get("pid"), entry.get("tty")
    if not historical and (type(pid) is not int or pid <= 0 or not isinstance(tty, str) or not tty):
        raise ValueError("closure requires the matched process PID and TTY")
    rows = inventory()
    if any(p == pid or (not entry.get("_backfill") and t == tty and not idle_terminal_command(c)) for p, t, c in rows):
        raise ValueError("source PID (possibly reused) or work on its TTY remains; preserve claims and verify native identity before the documented manual release-claim route")
    for _, process_tty, command in rows:
        # ps does not preserve shell quoting. Inspect only native resume
        # operands; arbitrary quoted prompt text is neither syntax nor identity.
        argv = command.split()
        resumed_ids = [argv[index + 1].strip("\"'") for index, arg in enumerate(argv)
                       if arg in {"resume", "--resume", "-r"} and index + 1 < len(argv)]
        resumed_ids.extend(arg.split("=", 1)[1] for arg in argv if arg.startswith("--resume="))
        if entry["session_id"] in resumed_ids:
            raise ValueError("source native session is visible in a resumed harness process")
        harness = any(Path(arg).name in {"codex", "claude", "claude.js"} for arg in argv)
        if entry.get("_backfill") and process_tty == tty and harness and not (
            len(resumed_ids) == 1 and resumed_ids[0] and not resumed_ids[0].startswith("-")
        ):
            raise ValueError("unidentified harness on the recorded TTY; preserve claims and verify native identity before the documented manual release-claim route")
    status = session_record.session_status(entry)
    if not status["safe_verdict"]:
        raise ValueError("latest transcript lacks an unqualified Safe to exit: yes")
    return status


def validate_closure(entry, supervisor_session):
    if entry.get("process_exit_verified") is not True:
        raise ValueError("closed-session log lacks verified process exit")
    if not isinstance(entry.get("ended_at"), str) or re.search(r"\s", entry["ended_at"]):
        raise ValueError("closure requires a single timezone-qualified ISO timestamp")
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
    records = [{k: v for k, v in e.items() if not k.startswith("_")}
               for e in session_record.load_ledger(path)] if path.exists() else []
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
                    for source in comments if source["id"] in active_ids
                    for r in claim.records(source.get("body") or ""))):
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


def discover_handoff(reader, entry, actor):
    """Use native transcript links first; never attribute a shared bot by recency alone."""
    records = session_record.read_jsonl(Path(entry["transcript"]))
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)
    # Shared-bot comments merely read by the source are not its handoff.
    outputs = []
    writes = set()
    write_command = re.compile(r"(?:gh-comment|github_comment\.py|gh-pr\.py\s+comment)\b[^\n;]*\b(?:issue|pr)?\s*[1-9]\d*")
    for record in records:
        payload = record.get("payload") or {}
        arguments = payload.get("arguments") or payload.get("input") or ""
        try:
            structured = json.loads(arguments) if isinstance(arguments, str) else arguments
        except ValueError:
            structured = None
        command = (structured.get("cmd") or structured.get("command") or "") if isinstance(structured, dict) else ""
        code = structured.get("code", "") if isinstance(structured, dict) else arguments
        if not command and isinstance(code, str):
            literals = re.findall(r'\bcmd\s*:\s*("(?:\\.|[^"\\])*")', code)
            command = "\n".join(json.loads(literal) for literal in literals)
        # Historical fixtures/native direct calls may contain the shell command itself.
        if not command and not isinstance(structured, dict) and payload.get("name") in {"exec_command", "functions.exec_command"}:
            command = str(arguments)
        if payload.get("type") in {"function_call", "custom_tool_call"} and write_command.search(command):
            if payload.get("call_id"):
                writes.add(payload["call_id"])
        if payload.get("type") in {"function_call_output", "custom_tool_call_output"} and payload.get("call_id") in writes:
            outputs.extend(strings(payload.get("output")))
        for block in (record.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and write_command.search(str((block.get("input") or {}).get("command") or (block.get("input") or {}).get("cmd") or "")):
                if block.get("id"):
                    writes.add(block["id"])
            if block.get("type") == "tool_result" and block.get("tool_use_id") in writes and block.get("is_error") is not True:
                outputs.extend(strings(block.get("content")))
    receipt_urls = []
    decoder = json.JSONDecoder()
    for text in outputs:
        # Consume complete envelopes, not URLs embedded in a subsequent read's body.
        cursor = 0
        while (start := text.find("{", cursor)) >= 0:
            try:
                value, length = decoder.raw_decode(text[start:])
                cursor = start + length
            except ValueError:
                cursor = start + 1
                continue
            if isinstance(value, dict) and value.get("comment_action") in {"created", "updated"}:
                url = (value.get("comment") or {}).get("url")
                if isinstance(url, str) and HANDOFF.fullmatch(url):
                    receipt_urls.append(url)
        # Older helpers were commonly piped to grep url; accept a single URL.
        legacy = list(dict.fromkeys(re.findall(HANDOFF.pattern, text)))
        if len(legacy) == 1 and "{" not in text:
            repo, number, comment_id = legacy[0]
            receipt_urls.append(f"https://github.com/{repo}/issues/{number}#issuecomment-{comment_id}")
    final_text = session_record.session_status(entry)["last_text"]
    final_urls = [f"https://github.com/{repo}/issues/{number}#issuecomment-{comment_id}"
                  for repo, number, comment_id in re.findall(HANDOFF.pattern, final_text)]
    candidates = [*entry.get("_handoffs", []),
                  *reversed(receipt_urls), *reversed(final_urls)]
    for url in dict.fromkeys(candidates):
        match = HANDOFF.fullmatch(url)
        if not match:
            continue
        try:
            evidence = handoff(reader, {**entry, "handoff_url": url}, actor)
        except ValueError:
            continue
        except GitHubReadError as error:
            if getattr(error.result, "status", None) in {403, 404}:
                continue
            raise
        text = evidence.get("body") or ""
        if (release.stamp(evidence.get("updated_at") or evidence.get("created_at")) <= release.stamp(entry["ended_at"])
                and not text.startswith("Claimed by ") and not claim.records(text)
                and (url in receipt_urls or entry["session_id"] in text)):
            provenance = "native comment-write receipt" if url in receipt_urls else "exact-ID handoff"
            return {**entry, "handoff_url": url, "_handoff_provenance": provenance}, evidence
    # A summary without a transcript URL must name the exact native ID.
    threads = {(r["repo"], r["issue"]) for r in entry.get("_inventory_claims", [])}
    if entry.get("_handoff_thread"):
        threads.add(entry["_handoff_thread"])
    for repo, number in sorted(threads):
        cache = entry.get("_handoff_cache", {})
        if (repo, number) not in cache:
            cache[(repo, number)] = reader.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="backfill_handoff")
        comments = cache[(repo, number)]
        for evidence in reversed(comments):
            text = evidence.get("body") or ""
            if ((evidence.get("user") or {}).get("login") == actor and entry["session_id"] in text
                    and not text.startswith("Claimed by ") and not claim.records(text)
                    and release.stamp(evidence.get("updated_at") or evidence.get("created_at")) <= release.stamp(entry["ended_at"])):
                url = evidence["html_url"]
                checked = handoff(reader, {**entry, "handoff_url": url}, actor)
                return {**entry, "handoff_url": url, "_handoff_provenance": "exact-ID summary"}, checked
    # Interrupted sessions may have no public closeout. The inventory's original
    # native ownership record is a recovery context, not a fabricated summary.
    for row in entry.get("_inventory_claims", []):
        comments = entry.get("_handoff_cache", {}).get((row["repo"], row["issue"]))
        if comments is None:
            comments = reader.paged_json(f"/repos/{row['repo']}/issues/{row['issue']}/comments", step_prefix="backfill_source_context")
        source = next((c for c in comments if c.get("id") == row["claim_comment_id"]), None)
        if source is None or (source.get("user") or {}).get("login") != actor:
            continue
        parsed = claim.records(source.get("body") or "")
        if (len(parsed) == 1 and parsed[0]["session"] == entry["session_id"]
                and release.stamp(source.get("updated_at") or source.get("created_at")) <= release.stamp(entry["ended_at"])):
            url = source.get("html_url") or f"https://github.com/{row['repo']}/issues/{row['issue']}#issuecomment-{source['id']}"
            checked = handoff(reader, {**entry, "handoff_url": url}, actor)
            return {**entry, "handoff_url": url, "_handoff_provenance": "original native ownership record; closeout verified separately"}, checked
    raise ValueError("no authored handoff bound to the exact native transcript/session; reconcile this session")


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
    status = validate_closure(entry, supervisor_session)
    if entry.get("handoff_url"):
        handoff_record = handoff(reader, entry, actor)
    else:
        entry, handoff_record = discover_handoff(reader, entry, actor)
    plans = []
    retained_status = []
    pulls_by_repo = {}
    handoff_prs = set(re.findall(r"https://github\.com/[^/\s]+/[^/\s]+/pull/[1-9]\d*", handoff_record.get("body") or ""))
    refused = []
    for repo, number in issues:
        if entry.get("_inventory") and (repo, number) not in {(r["repo"], r["issue"]) for r in entry["_inventory_claims"]}:
            continue
        try:
            issue_plans, status_claims = plan_issue(reader, repo, number, entry, actor, handoff_prs, pulls_by_repo, checkout_root, apply)
            plans.extend(issue_plans)
            retained_status.extend(status_claims)
        except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError, GitHubReadError, GitHubReadShapeError) as error:
            refused.append({"repository": repo, "issue": number, "error": str(error)})
    report = {"session_id": entry["session_id"], "claims": [], "dry_run": not apply,
              "retained_status_claims": retained_status, "issue_refusals": refused,
              "inventory_claims": list(entry.get("_inventory_results", {}).values()),
              "handoff_url": entry["handoff_url"],
              "closure_evidence": "Supervisor verified inventory; original PID/TTY unavailable" if entry.get("_inventory") else "matched PID/TTY log"}
    try:
        return apply_plans(entry, supervisor_session, plans, status, checkout_root, actor, apply, report, reader)
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError, CommentError) as error:
        raise PartialRelease(error, report) from error


def plan_issue(reader, repo, number, entry, actor, handoff_prs: set[str], pulls_by_repo, checkout_root, apply):
    plans, retained_status = [], []
    reader.deadline_at = time.time() + entry.get("_read_deadline_seconds", 600)
    issue = reader.get_json(f"/repos/{repo}/issues/{number}", step="release_canonical_issue")
    if "pull_request" in issue:
        if entry.get("_inventory"):
            raise ValueError("inventory names a pull request instead of its canonical issue")
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
        remote = claim.run_read(["git", "remote", "get-url", "origin"], cwd=checkout_root / repo.split("/")[1]).strip()
        if not re.search(rf"[:/]{re.escape(repo)}(?:\.git)?$", remote, re.IGNORECASE):
            raise ValueError("release checkout must match the canonical repository")
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


def apply_plans(entry, supervisor_session, plans, status, checkout_root, actor, apply, report, reader):
    for repo, number, source_id, related, prs in plans:
        if validate_closure(entry, supervisor_session) != status:
            raise ValueError("source transcript changed; preserve remaining claims")
        body = (f"Closed session {entry['session_id']}\nEnded at: {entry['ended_at']}\nSafe to exit: yes\n\n"
                f"Handoff context ({entry.get('_handoff_provenance', 'Supervisor transcript-verified ledger')}): {entry['handoff_url']}\n")
        if entry.get("_inventory"):
            body += "Closure evidence: Supervisor verified inventory; original PID/TTY unavailable. The caller confirms actual session inactivity, including unavailable native peers.\n"
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
    parser.add_argument("--handoff-thread", help="fallback summary thread OWNER/REPO#NUMBER; default OWNER/codex-skills#884")
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
            entries = [{k: v for k, v in e.items() if not k.startswith("_")}
                       for e in session_record.load_ledger(args.ledger) if e["session_id"] == args.session_id]
            if len(entries) != 1:
                raise ValueError("native session must match exactly one ledger entry")
            entries[0]["_transcript_roots"] = transcript_roots(entries[0]["harness"], args.account_config, args.transcript_root)
            verify_closed(entries[0], args.supervisor_session)
            if args.apply:
                entries = [record_closure(args.closed_log, entries[0], args.supervisor_session)]
            else:
                entries = [record_closure(args.closed_log, entries[0], args.supervisor_session, persist=False)]
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
                entries = [{k: v for k, v in e.items() if not k.startswith("_")}
                           for e in session_record.load_ledger(args.closed_log)]
            entries = [{**e, "_backfill": True} for e in entries]
            if args.session_id:
                entries = [e for e in entries if e["session_id"] == args.session_id]
            if not entries:
                raise ValueError("closed-session log has no matching records")
        thread = re.fullmatch(r"([^#]+)#([1-9]\d*)", args.handoff_thread or f"{args.owner}/codex-skills#884")
        if not thread or not REPO.fullmatch(thread[1]) or thread[1].split("/")[0].casefold() != args.owner.casefold():
            raise ValueError("handoff thread must be a canonical thread under the requested owner")
        entries = [{**e, "_handoff_thread": (thread[1], int(thread[2])),
                    "_transcript_roots": e.get("_transcript_roots") or transcript_roots(e["harness"], args.account_config, args.transcript_root),
                    "_read_deadline_seconds": args.deadline_seconds} for e in entries]
        verified = []
        handoff_cache = {}
        for entry in entries:
            try:
                entry["_handoff_cache"] = handoff_cache
                validate_closure(entry, args.supervisor_session)
                verified.append(entry)
            except (ValueError, KeyError, TypeError, OSError) as error:
                reports.append({"session_id": entry["session_id"], "error": str(error), "released": False,
                                "inventory_claims": [{"repository": r["repo"], "issue": r["issue"],
                                    "claim_comment": r["claim_comment_id"], "status": "preserved"}
                                    for r in entry.get("_inventory_claims", [])]})
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
        ok = not any("error" in r or r.get("issue_refusals") for r in reports)
        print(json.dumps({"ok": ok, "reports": reports, "repositories_checked": repos,
                          "needs_reconciliation": any(r.get("retained_status_claims") or r.get("issue_refusals") or "error" in r for r in reports),
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
