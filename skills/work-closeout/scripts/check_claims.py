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
import claim_history


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


def check_record(reader, repo, number, session, actor, *, pulls_cache=None, created_prs=(), data_cache=None):
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
        evidence = next((c for c in reversed(comments[comments.index(source) + 1:])
                         if claim.exact_released_sources([source, c])), None)
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
            pulls = [p for p in pulls if (p.get("head") or {}).get("ref") == record["branch"] or p["number"] in named
                     or p.get("html_url") in created_prs and claim.pr_issue_reference(p, number, repo=repo, attestation=True)]
        if receipt is not None:
            for pull in pulls:
                if pull["html_url"] not in receipt["checked_prs"]:
                    raise ValueError("retained PR is absent from the verified recovery release")
                head = reader.get_json(f"/repos/{repo}/commits/{pull['head']['sha']}", step="recovered_retained_pr_head")
                release.check_retained_pr(pull, head, repo=repo, number=number, actor=actor,
                    ended=receipt["evidence_at"], subject_pr=number if is_pr else None, source_branch=record["branch"])
            handoffs = [evidence]
        else:
            handoffs = retained_handoff(comments, source, pulls, repo=repo, number=number, is_pr=is_pr) if pulls else [evidence]
        result.append({"repository": repo, "record": number, "claim_comment": source["id"], "source_session": session,
                       "worker": record["worker"], "branch": record["branch"],
                       "release_url": f"https://github.com/{repo}/issues/{number}#issuecomment-{evidence['id']}",
                       "handoff_urls": [f"https://github.com/{repo}/issues/{number}#issuecomment-{h['id']}" for h in handoffs],
                       "retained_prs": [p["html_url"] for p in pulls]})
    return result


def check(reader, owner, entry, actor, *, reader_for_repo=None):
    # No caller-supplied narrowed issue/repo list may turn partial discovery
    # into an all-clear. Use the existing paged, non-search inventory.
    histories = entry.get("_claim_histories") or [{"session_id": entry["session_id"], "harness": entry["harness"],
                                                "records": entry.get("_transcript_records", [])}]
    file_candidates = claim_receipts.ownership_file_candidates(histories)
    if any(claim_receipts.ownership_state(node["records"], node["harness"], file_candidates=file_candidates)["pending"] for node in histories):
        raise ValueError("ownership command has no terminal native result; wait for it to finish, then launch a new check")
    native_sessions = {node["session_id"] for node in histories}
    expected, errors = [], []
    for node in histories:
        for native in native_sessions:
            found, missing = claim_receipts.sources(node["records"], node["harness"], native)
            expected.extend(found)
            errors.extend(missing)
    native_repos = {r["repository"] for r in expected}
    native_issues = {(r["repository"], r["record"]) for r in expected}
    for node in histories:
        # Native process handles are local to their transcript, even when two
        # delegated histories happen to reuse the same numeric ID.
        for _, value in claim_receipts.outputs(node["records"], node["harness"]):
            if value.get("operation") == claim_receipts.CLAIM_OPERATION:
                repo = (value.get("issue") or {}).get("repo")
                if isinstance(repo, str) and sessions.REPO.fullmatch(repo):
                    native_repos.add(repo)
                    number = (value.get("issue") or {}).get("number")
                    if type(number) is int and number > 0:
                        native_issues.add((repo, number))
        native_repos.update(claim_receipts.target_repositories(node["records"], node["harness"], allowed_sessions=native_sessions))
    native_repos.update(entry.get("_extra_repositories", []))
    targets = [t for node in histories for t in claim_receipts.claim_targets(node["records"], node["harness"], allowed_sessions=native_sessions)]
    unresolved = [t for t in targets if t["repository"] is None]
    if unresolved and not entry.get("_extra_repositories"):
        raise ValueError("claim invocation lacks canonical command scope; --repository may add inventory but cannot prove its destination; use conditional/no")
    expanded = set()
    for target in targets:
        repo, number = target["repository"], target["number"]
        if number is None:
            continue  # Unknown operands stay incomplete without guessed API paths.
        if repo:
            native_issues.add((repo, number))
        else:
            expanded.update((context, number) for context in entry.get("_extra_repositories", []))
    confirmed_issues = set(native_issues)
    native_issues.update(expanded)
    installed = reader.paged_json("/installation/repositories", collection_key="repositories", step_prefix="closeout_default_repositories")
    default_repos = {r["full_name"] for r in installed}
    if not default_repos:
        raise ValueError("default installation repository inventory is empty")
    owners = {owner} if isinstance(owner, str) else set(owner)
    owners.update(r.split("/")[0] for r in default_repos | native_repos)
    activity = any(list(claim_receipts.ownership_activity(node["records"], node["harness"])) for node in histories)
    if (errors or not expected and activity) and not native_repos:
        raise ValueError("claim receipt lacks repository context; add its exact --repository before reconciliation")
    # Renewal/uncertain-write receipts can refer to a source older than this
    # transcript prefix. Their canonical record must be read independently of
    # the since-filtered discovery window.
    issues, repos = set(native_issues), set()
    readers = {}
    for account in sorted(owners):
        scope = {r for r in default_repos | native_repos if r.split("/")[0].casefold() == account.casefold()}
        if not scope:
            raise ValueError("account has no installed or native repository context; add its exact --repository")
        for repo in sorted(scope):
            selected = reader if repo in default_repos else (reader_for_repo or repository_reader)(repo, reader.deadline_at)
            readers[repo] = selected
            entries = ([{**entry, "session_id": node["session_id"], "_transcript_records": node["records"]} for node in histories]
                       if "_transcript_records" in entry else [entry])
            found, _ = sessions.discover(selected, account, entries, repos=[repo])
            issues.update(found)
            repos.add(repo)
    released, blocked, pulls_cache, data_cache = [], [], {}, {}
    created = set().union(*(claim_receipts.created_prs(node["records"], node["harness"]) for node in histories))
    for repo, number in sorted(issues):
        try:
            selected = readers[repo]
            source_actor = selected.expected_actor
            if not source_actor:
                raise ValueError("repository's configured automation identity is unknown")
            for native in sorted(native_sessions):
                released.extend(check_record(selected, repo, number, native, source_actor,
                                             pulls_cache=pulls_cache, created_prs=created, data_cache=data_cache))
        except ValueError as error:
            blocked.append(str(error))
        except GitHubReadError as error:
            if (repo, number) in expanded - confirmed_issues and getattr(error.result, "status", None) in {404, 410}:
                continue  # A guessed pair is not source evidence; still reconcile below.
            raise
    observed = {(r["repository"], r["record"], r["claim_comment"]) for r in released}
    if any((r["repository"], r["record"], r["claim_comment"]) not in observed for r in expected) and not blocked:
        blocked.append("native source claims were not verified in the installed repository scope")
    for target in unresolved:
        # An older claim with the same identity/number cannot establish where
        # this opaque invocation wrote. Added repositories are inventory scope,
        # not an attestation of the invocation's unknown working directory.
        blocked.append(f"claim invocation {target['invocation']} lacks canonical command scope; added repositories cannot prove its destination; preserve ownership and use conditional/no")
    return {"operation": claim_receipts.CHECK_OPERATION, "ok": not blocked, "claims_clear": not blocked,
            "session_id": entry["session_id"], "harness": entry["harness"],
            "coverage": {"owners": sorted(owners), "repositories": sorted(repos), "complete": not unresolved,
                         "session_ids": sorted(native_sessions),
                         "histories": [{k: node[k] for k in ("session_id", "transcript", "digest")} for node in histories[1:]],
                         "scope": "default installation plus native and explicitly added repositories"},
            "receipt_reconciliation": not blocked,
            "released_claims": released, "blocked": blocked}


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
        reader = GitHubReader(expected_actor=actor, gh_prefix_args=["--require-automation-auth"], deadline_at=time.time() + 600, cache_revalidate=True)
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
