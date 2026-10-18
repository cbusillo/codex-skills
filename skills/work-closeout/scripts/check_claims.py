#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read back a native session's claim dispositions before a safe-exit verdict.

This never releases ownership or closes a session. Native claim receipts derive
the GitHub accounts to inventory, including closed issues and PR-only claims.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time

SKILLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS / "github/scripts"))
sys.path.insert(0, str(SKILLS / "supervisor/scripts"))
import github_agent
import github_identity
import github_plan_claim as claim
import github_plan_release as release
from github_read import GitHubReader, GitHubReadError, GitHubReadShapeError
import release_session_claims as sessions
import session_record
import claim_receipts


def transcript_snapshot(path, harness, session):
    # Read one immutable prefix. A running harness may append a partial final
    # record; never weaken the strict ended-session reader used for shutdown.
    lines = path.read_bytes().splitlines(keepends=True)
    if lines and not lines[-1].endswith(b"\n"):
        lines.pop()
    records = [json.loads(line) for line in lines if line.strip()]
    for record in records:
        session_record.validate_record(record)
    identities = ({r.get("sessionId") for r in records if r.get("sessionId")} if harness == "claude" else
                  {r.get("payload", {}).get("id") for r in records if r.get("type") == "session_meta"})
    if identities != {session}:
        raise ValueError("native transcript identity is unavailable or differs")
    return records


def explicit_pr_numbers(text, repo):
    # Bare issue references are dependencies, not assertions of retained PRs.
    return {int(n) for n in re.findall(rf"https://github\.com/{re.escape(repo)}/pull/([1-9]\d*)", text)} | {
        int(n) for n in re.findall(r"\bPR\s*#?([1-9]\d*)\b", text, re.I)}


def retained_handoff(comments, source, pulls, *, repo, number, is_pr):
    """Use the successor's own handoff contract, not a second prose heuristic."""
    candidates = [c for c in comments if (c.get("user") or {}).get("login") ==
                  (source.get("user") or {}).get("login")]
    verified = []
    for pull in pulls:
        for handoff in reversed(candidates):
            try:
                if is_pr:
                    # A PR-only claim need not link itself in its body. Its own
                    # same-repository PR is already the canonical work record. A
                    # refresh claim's task branch can differ from its remote head.
                    named = explicit_pr_numbers(handoff.get("body") or "", repo)
                    if number not in named or comments.index(handoff) <= comments.index(source):
                        continue
                    if claim.released_claim_id(handoff.get("body") or "") != source["id"]:
                        continue
                    if (pull["number"] != number
                            or any(pull[side]["repo"]["full_name"].casefold() != repo.casefold()
                                   for side in ("head", "base"))):
                        raise ValueError("retained PR claim no longer matches its repository")
                else:
                    claim.retained_handoff(comments, source["id"], handoff["id"], pulls, pull["number"],
                        issue_repo=repo, issue_number=number, target_repo=repo)
                verified.append(handoff)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"claim {source['id']} lacks a verified retained-PR release/handoff")
    return verified


def check_record(reader, repo, number, session, actor, *, pulls_cache=None, created_prs=()):
    issue = reader.get_json(f"/repos/{repo}/issues/{number}", step="closeout_claim_record")
    comments = reader.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="closeout_claim_history")
    sources = [c for c in comments if any(r["session"] == session for r in claim.records(c.get("body") or ""))]
    if sessions.active_claims(reader, repo, number, session, actor, comments=comments):
        raise ValueError(f"{repo}#{number}: session still holds a claim; post its exact authored release and read it back")
    body_records = [r for r in claim.records(issue.get("body") or "") if r["session"] == session]
    if any(r not in [r for c in sources for r in claim.records(c.get("body") or "")] for r in body_records):
        raise ValueError(f"{repo}#{number}: status ownership has no matching source claim")
    if not sources and sessions.belongs_to_session(issue.get("body") or "", session):
        raise ValueError(f"{repo}#{number}: status-only ownership needs reconciliation")
    exact = {comments[i]["id"]: comments[j] for i, record, j in claim.exact_released_sources(comments)
             if record["session"] == session}
    result = []
    for source in sources:
        record = release.source_record(source)
        evidence = exact.get(source["id"])
        if ((source.get("user") or {}).get("login") != actor or evidence is None
                or release.receipt_for(evidence) is not None
                or not release.related_sessions_match(evidence.get("body") or "", session)):
            raise ValueError(f"{repo}#{number}: claim {source['id']} lacks an exact source-authored release")
        is_pr = "pull_request" in issue
        if is_pr:
            pull = reader.get_json(f"/repos/{repo}/pulls/{number}", step="closeout_claim_pr")
            pulls = [pull] if pull["state"] == "open" else []
        else:
            if pulls_cache is None:
                pulls_cache = {}
            if repo not in pulls_cache:
                pulls_cache[repo] = reader.paged_json(f"/repos/{repo}/pulls", params={"state": "open"}, step_prefix="closeout_retained_prs")
            pulls = pulls_cache[repo]
            named = set()
            for c in comments[comments.index(evidence):]:
                if ((c.get("user") or {}).get("login") == actor and
                        ((c.get("body") or "").startswith(f"Handoff from {record['worker']}\n") or c == evidence)):
                    named.update(explicit_pr_numbers(c.get("body") or "", repo))
            pulls = [p for p in pulls if (p.get("head") or {}).get("ref") == record["branch"] or p["number"] in named
                     or p.get("html_url") in created_prs and claim.pr_issue_reference(p, number, repo=repo, attestation=True)]
        handoffs = retained_handoff(comments, source, pulls, repo=repo, number=number, is_pr=is_pr) if pulls else [evidence]
        result.append({"repository": repo, "record": number, "claim_comment": source["id"],
                       "release_url": f"https://github.com/{repo}/issues/{number}#issuecomment-{evidence['id']}",
                       "handoff_urls": [f"https://github.com/{repo}/issues/{number}#issuecomment-{h['id']}" for h in handoffs],
                       "retained_prs": [p["html_url"] for p in pulls]})
    return result


def check(reader, owner, entry, actor):
    # No caller-supplied narrowed issue/repo list may turn partial discovery
    # into an all-clear. Use the existing paged, non-search inventory.
    expected, errors = claim_receipts.sources(entry.get("_transcript_records", []), entry["harness"], entry["session_id"])
    if errors:
        raise ValueError("claim receipt inventory is incomplete")
    owners = {owner} if isinstance(owner, str) else set(owner)
    owners.update(r["repository"].split("/")[0] for r in expected)
    issues, repos = set(), set()
    for account in sorted(owners):
        found, installed = sessions.discover(reader, account, [entry])
        issues.update(found)
        repos.update(installed)
    released, blocked, pulls_cache = [], [], {}
    created = claim_receipts.created_prs(entry.get("_transcript_records", []), entry["harness"])
    for repo, number in sorted(issues):
        try:
            released.extend(check_record(reader, repo, number, entry["session_id"], actor,
                                         pulls_cache=pulls_cache, created_prs=created))
        except ValueError as error:
            blocked.append(str(error))
    observed = {(r["repository"], r["record"], r["claim_comment"]) for r in released}
    if any((r["repository"], r["record"], r["claim_comment"]) not in observed for r in expected) and not blocked:
        blocked.append("native source claims were not verified in the installed repository scope")
    return {"operation": claim_receipts.CHECK_OPERATION, "ok": not blocked, "claims_clear": not blocked,
            "session_id": entry["session_id"], "harness": entry["harness"],
            "coverage": {"owners": sorted(owners), "repositories": sorted(repos), "complete": True},
            "released_claims": released, "blocked": blocked}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", action="append", default=[], help="Additional GitHub account to inventory; native claim receipts always add their accounts")
    parser.add_argument("--harness", required=True, choices=github_agent.FAMILIES)
    parser.add_argument("--session", required=True)
    parser.add_argument("--transcript", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner) for owner in args.owner):
            raise ValueError("owner must be a GitHub login")
        if github_agent.running_agent() != args.harness:
            raise ValueError("requested harness differs from the running harness")
        native = github_agent.native_sessions(args.harness)
        if not native or args.session not in native.values():
            raise ValueError("closeout must use the running harness's native session ID")
        entry = {"harness": args.harness, "session_id": args.session, "transcript": str(args.transcript)}
        entry["_transcript_records"] = transcript_snapshot(args.transcript, args.harness, args.session)
        actor = github_identity.automation_login()
        reader = GitHubReader(expected_actor=actor, deadline_at=time.time() + 600, cache_revalidate=True)
        report = check(reader, args.owner, entry, actor)
        report["checked_at"] = time.time()
        print(json.dumps(report, sort_keys=True))
        return 0 if report["claims_clear"] else 1
    except (ValueError, OSError, TypeError, KeyError, GitHubReadError, GitHubReadShapeError) as error:
        print(json.dumps({"operation": claim_receipts.CHECK_OPERATION,
                          "session_id": args.session, "harness": args.harness,
                          "ok": False, "claims_clear": False, "coverage": {"complete": False},
                          "error": str(error), "next_action": "Preserve ownership, record the blocker, and use conditional/no; do not report safe exit yes."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
