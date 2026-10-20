#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///
"""Read back a native session's claim dispositions before a safe-exit verdict.

This never releases ownership or closes a session. Every configured writer App
installation is inventoried, including closed issues and PR-only claims.
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
import claim_history
import github_plan_sessions as native_ownership


def transcript_snapshot(path, harness, session, context=None):
    # Read one immutable prefix. A running harness may append a partial final
    # record; never weaken the strict ended-session reader used for shutdown.
    lines = path.read_bytes().splitlines(keepends=True)
    if lines and not lines[-1].endswith(b"\n"):
        lines.pop()
    records = [json.loads(line) for line in lines if line.strip()]
    for record in records:
        session_record.validate_record(record)
    if context is None:
        return claim_history.root_records(path, harness, session, records)
    claim_history.child_records(records, harness, session, context)
    return records


def receipt_history(path, harness, session):
    records = transcript_snapshot(path, harness, session)
    return [r for node in claim_history.collect(path, harness, session, records, transcript_snapshot) for r in node["records"]]


def explicit_pr_numbers(text, repo):
    # Bare issue references are dependencies, not assertions of retained PRs.
    return {int(n) for n in re.findall(rf"https://github\.com/{re.escape(repo)}/pull/([1-9]\d*)", text)}


def retained_handoff(comments, source, pulls, *, repo, number, is_pr):
    """Use the successor's own handoff contract, not a second prose heuristic."""
    candidates = [c for c in comments if (c.get("user") or {}).get("login") ==
                  (source.get("user") or {}).get("login")]
    if not is_pr:
        released = claim.released_source(comments, source["id"])
        candidates = claim.handoff_candidates(comments, released)
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
                    # Closeout covers every pull, including separate authored
                    # handoffs. A successor selects just one of those handoffs.
                    claim.retained_handoff(released, [handoff], [pull],
                        issue_repo=repo, issue_number=number, target_repo=repo, selected_id=handoff["id"])
                verified.append(handoff)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"claim {source['id']} lacks a verified retained-PR release/handoff")
    return verified


def check_record(reader, repo, number, session, actor, *, pulls_cache=None, data_cache=None):
    if data_cache is None:
        data_cache = {}
    if (repo, number) not in data_cache:
        issue = reader.get_json(f"/repos/{repo}/issues/{number}", step="closeout_claim_record")
        comments = reader.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="closeout_claim_history")
        data_cache[(repo, number)] = issue, sessions.verified_comments(reader, comments)
    issue, comments = data_cache[(repo, number)]
    sources = [c for c in comments if any(r["session"] == session for r in claim.records(c.get("body") or ""))]
    if sessions.active_claims(reader, repo, number, session, actor, comments=comments, verified=True):
        raise ValueError(f"{repo}#{number}: session still holds a claim; post its exact authored release and read it back")
    body_records = [r for r in claim.records(issue.get("body") or "") if r["session"] == session]
    if any(r not in [r for c in sources for r in claim.records(c.get("body") or "")] for r in body_records):
        raise ValueError(f"{repo}#{number}: status ownership has no matching source claim")
    if not sources and sessions.belongs_to_session(issue.get("body") or "", session):
        raise ValueError(f"{repo}#{number}: status-only ownership needs reconciliation")
    result = []
    for source in sources:
        record = release.source_record(source)
        releases = [c for c in comments[comments.index(source) + 1:]
                    if claim.exact_released_sources([source, c])]
        evidence = releases[-1] if releases else None
        if ((source.get("user") or {}).get("login") != actor or evidence is None):
            raise ValueError(f"{repo}#{number}: claim {source['id']} lacks an exact source-authored release")
        receipt = release.receipt_for(evidence)
        if receipt is None and not release.related_sessions_match(evidence.get("body") or "", session):
            raise ValueError(f"{repo}#{number}: release names another native session")
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
            def during_claim(pull):
                if (not claim.pr_issue_reference(pull, number, repo=repo, attestation=True)
                        or (pull.get("user") or {}).get("login") != actor):
                    return False
                try:
                    return (release.stamp(source["created_at"]) <= release.stamp(pull.get("created_at"))
                            <= release.stamp(releases[0]["created_at"]))
                except (TypeError, ValueError, KeyError):
                    # Unknown chronology cannot hide an independently linked PR.
                    return True
            pulls = [p for p in pulls if (p.get("head") or {}).get("ref") == record["branch"]
                     or p["number"] in named or during_claim(p)]
        if receipt is not None:
            handoffs = [evidence]
            for pull in pulls:
                if pull["html_url"] not in receipt["checked_prs"]:
                    handoffs.extend(retained_handoff(comments, source, [pull],
                        repo=repo, number=number, is_pr=is_pr))
                    continue
                head = reader.get_json(f"/repos/{repo}/commits/{pull['head']['sha']}", step="recovered_retained_pr_head")
                release.check_retained_pr(pull, head, repo=repo, number=number, actor=actor,
                    ended=receipt["evidence_at"], subject_pr=number if is_pr else None, source_branch=record["branch"])
        else:
            handoffs = retained_handoff(comments, source, pulls, repo=repo, number=number, is_pr=is_pr) if pulls else [evidence]
        result.append({"repository": repo, "record": number, "claim_comment": source["id"], "source_session": session,
                       "worker": record["worker"], "branch": record["branch"],
                       "release_url": f"https://github.com/{repo}/issues/{number}#issuecomment-{evidence['id']}",
                       "handoff_urls": [f"https://github.com/{repo}/issues/{number}#issuecomment-{h['id']}" for h in handoffs],
                       "retained_prs": [p["html_url"] for p in pulls]})
    return result


def check(reader, owner, entry, actor, *, reader_for_repo=None, read_timeout=None):
    histories = entry.get("_claim_histories") or [{"session_id": entry["session_id"], "harness": entry["harness"],
                                                "records": entry.get("_transcript_records", [])}]
    states = [claim_receipts.ownership_state(node["records"], node["harness"]) for node in histories]
    native_sessions = {node["session_id"] for node in histories}
    expected = []
    receipt_issues = set()
    for node in histories:
        parsed = list(claim_receipts.outputs(node["records"], node["harness"]))
        for _, value in parsed:
            if value.get("operation") != claim_receipts.CLAIM_OPERATION:
                continue
            issue, identity = value.get("issue") or {}, value.get("claim") or {}
            if not isinstance(issue, dict) or not isinstance(identity, dict):
                continue  # Canonical inventory still reconciles every source.
            repo, number = issue.get("repo"), issue.get("number")
            if (identity.get("session") in native_sessions
                    and isinstance(repo, str) and sessions.REPO.fullmatch(repo)
                    and type(number) is int and number > 0):
                receipt_issues.add((repo, number))
        for native in native_sessions:
            found, _ = claim_receipts.sources(node["records"], node["harness"], native, parsed=parsed)
            expected.extend(found)
    if read_timeout is not None:
        reader.deadline_at = time.time() + read_timeout
    required_repos = ({r["repository"] for r in expected} | {repo for repo, _ in receipt_issues}
                      | set(entry.get("_extra_repositories", [])))
    entries = [{**entry, "session_id": node["session_id"], "_transcript_records": node["records"]} for node in histories]
    try:
        since = release.stamp(sessions.scan_start(entries))
    except (ValueError, TypeError):
        since = None  # Unknown history can never justify a suspension exclusion.
    inventory = github_identity.automation_repositories(deadline_at=reader.deadline_at,
        since=since, required_repositories=required_repos)
    writers = inventory["repositories"]
    repos = set(writers) | required_repos
    owners = {owner} if isinstance(owner, str) else set(owner)
    owners.update(r.split("/")[0] for r in repos)
    known_accounts = {repo.split("/")[0].casefold() for repo in repos}
    known_accounts.update(row["account"]["login"].casefold() for row in inventory["installations"])
    if any(account.casefold() not in known_accounts for account in owners):
        raise ValueError("required account has no verified repository inventory")
    issues = {(r["repository"], r["record"]) for r in expected} | receipt_issues
    readers = {}
    blocked = [f"installation {row['installation_id']}: ownership inventory pending: {row['reason']}"
               for row in inventory.get("pending_installations", [])]
    complete = not blocked
    for repo in sorted(repos):
        try:
            selected = (reader_for_repo or repository_reader)(repo, reader.deadline_at)
            readers[repo] = selected
            if repo in writers and selected.expected_actor not in writers[repo]:
                raise ValueError("configured repository writer differs from verified App installation")
            found, _ = sessions.discover(selected, repo.split("/")[0], entries, repos=[repo])
            issues.update(found)
        except (ValueError, GitHubReadError, GitHubReadShapeError, github_identity.GitHubAppError) as error:
            blocked.append(f"{repo}: ownership inventory pending: {error}")
            complete = False
    released, pulls_cache, data_cache = [], {}, {}
    for repo, number in sorted(issues):
        try:
            selected = readers.get(repo)
            if selected is None:
                raise ValueError("repository inventory is incomplete")
            source_actor = selected.expected_actor
            if not source_actor:
                raise ValueError("repository's configured automation identity is unknown")
            for native in sorted(native_sessions):
                released.extend(check_record(selected, repo, number, native, source_actor,
                                             pulls_cache=pulls_cache, data_cache=data_cache))
        except (ValueError, GitHubReadError, GitHubReadShapeError) as error:
            blocked.append(f"{repo}#{number}: {error}")
            if not isinstance(error, ValueError):
                complete = False
    observed = {(r["repository"], r["record"], r["claim_comment"]) for r in released}
    if any((r["repository"], r["record"], r["claim_comment"]) not in observed for r in expected) and not blocked:
        blocked.append("native source claims were not verified in the installed repository scope")
    pending = native_ownership.pending_ownership(histories, states, deadline_at=reader.deadline_at)
    blocked.extend(f"{item['session_id']}: {item['reason']}" for item in pending)
    return {"operation": claim_receipts.CHECK_OPERATION, "ok": not blocked, "claims_clear": not blocked,
            "session_id": entry["session_id"], "harness": entry["harness"],
            "coverage": {"owners": sorted(owners), "repositories": sorted(repos), "complete": complete,
                         "installation_count": len(inventory["installations"]),
                         "excluded_installations": inventory.get("excluded_installations", []),
                         "session_ids": sorted(native_sessions),
                         "histories": [{k: node[k] for k in ("session_id", "transcript", "digest")} for node in histories[1:]],
                         "scope": "all configured automation writer App installations plus explicitly added repositories"},
            "receipt_reconciliation": not blocked,
            "released_claims": released, "pending": pending, "blocked": blocked}


def repository_reader(repo, deadline):
    # The shared reader selects the existing App by target repository, exactly
    # as the claim helper does. No credential/config mutation or fallback.
    return GitHubReader(repository=repo, expected_actor=github_identity.automation_login(),
                        gh_prefix_args=["--require-automation-auth"], deadline_at=deadline, cache_revalidate=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", action="append", default=[], help="Require account coverage; absent repository context refuses instead of narrowing native scope")
    parser.add_argument("--repository", action="append", default=[], help="Add exact repository context for a missing receipt or another installation")
    parser.add_argument("--harness", required=True, choices=github_agent.FAMILIES)
    parser.add_argument("--session", required=True)
    parser.add_argument("--transcript", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner) for owner in args.owner):
            raise ValueError("owner must be a GitHub login")
        if any(not sessions.REPO.fullmatch(repo) for repo in args.repository):
            raise ValueError("repository must use ACCOUNT/REPOSITORY form")
        if github_agent.running_agent() != args.harness:
            raise ValueError("requested harness differs from the running harness")
        native = github_agent.native_sessions(args.harness)
        if not native or args.session not in native.values():
            raise ValueError("closeout must use the running harness's native session ID")
        transcript = args.transcript.expanduser().resolve()
        entry = {"harness": args.harness, "session_id": args.session, "transcript": str(transcript)}
        root_records = transcript_snapshot(transcript, args.harness, args.session)
        entry["_claim_histories"] = claim_history.collect(transcript, args.harness, args.session, root_records, transcript_snapshot)
        entry["_transcript_records"] = [r for node in entry["_claim_histories"] for r in node["records"]]
        entry["_extra_repositories"] = args.repository
        actor = github_identity.automation_login()
        if not actor:
            raise ValueError("configured automation identity is unavailable")
        reader = GitHubReader(expected_actor=actor, gh_prefix_args=["--require-automation-auth"], cache_revalidate=True)
        report = check(reader, args.owner, entry, actor, read_timeout=600)
        report["checked_at"] = time.time()
        print(json.dumps(report, sort_keys=True))
        return 0 if report["claims_clear"] else 1
    except (ValueError, OSError, TypeError, KeyError, GitHubReadError, GitHubReadShapeError, github_identity.GitHubAppError) as error:
        print(json.dumps({"operation": claim_receipts.CHECK_OPERATION,
                          "session_id": args.session, "harness": args.harness,
                          "ok": False, "claims_clear": False, "coverage": {"complete": False},
                          "error": str(error), "next_action": "Preserve ownership, record the blocker, and use conditional/no; do not report safe exit yes."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
