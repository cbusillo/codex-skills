#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["tree-sitter==0.26.0", "tree-sitter-bash==0.25.1", "tree-sitter-javascript==0.25.0"]
# ///
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

SKILLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS / "github/scripts"))
sys.path.insert(0, str(SKILLS / "supervisor/scripts"))
import claim_transcripts as history
import github_identity
import github_plan_claim as claim
import github_plan_claim_log as claim_log
import github_plan_release as release
import release_session_claims as sessions
from github_read import GitHubReader, GitHubReadError, GitHubReadShapeError

CHECK_OPERATION = "work-closeout.claims"
JOURNAL_SHIPPED = datetime(2026, 10, 22, 3, 29, 6, tzinfo=UTC)


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


transcript_values = history.values


def entry_rows(entry: dict) -> list[dict]:
    return entry.get("_transcript_records") or history.read_records(Path(entry["transcript"]))


def receipt_values(rows: list[dict]) -> Iterator[dict]:
    receipts = [row for row in rows if "claim" in row]
    receipts.extend(block for blocks in history.tool_outputs(rows).values() for block in blocks)
    for block in receipts:
        yield from transcript_values(block)


def receipt_targets(rows: list[dict], native: str) -> set[tuple[str, int]]:
    return {target for value in receipt_values(rows) if (target := history.claim_target(value, native)) is not None}


def foreign_receipt(rows: list[dict], native: str) -> bool:
    return any(isinstance(claim := value.get("claim"), dict) and claim.get("session") != native for value in receipt_values(rows))


def session_start(rows: list[dict]) -> datetime | None:
    first = history.metadata(rows) or next((row for row in rows if "uuid" in row), {})
    try:
        began = datetime.fromisoformat(first["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None
    return began if began.tzinfo is not None else None


def lineage_start(entries: list[dict]) -> datetime | None:
    starts = [session_start(entry_rows(entry)) for entry in entries]
    return None if None in starts else min(starts, default=None)


class JournalEvidence(NamedTuple):
    journal: set[tuple[str, int]]
    seen: set[tuple[str, int]]
    attempted: frozenset[tuple[str, int]]


def journal_covers(rows: list[dict], native: str, began: datetime | None, evidence: JournalEvidence) -> bool:
    return (
        began is not None
        and began > JOURNAL_SHIPPED
        and evidence.seen <= evidence.journal <= evidence.attempted
        and not foreign_receipt(rows, native)
    )


def recorded_targets(entry: dict, began: datetime | None = None, attempted: frozenset[tuple[str, int]] = frozenset()) -> dict:
    require_own_transcript(entry)
    native = entry["session_id"]
    journal = claim_log.read(native)
    rows = entry_rows(entry)
    receipts = receipt_targets(rows, native)
    discovery = history.literal_targets(
        rows, native, directory=Path(entry["native_output_directory"]) if entry.get("native_output_directory") else None
    )
    discovery["journal_covered_invocations"] = []
    if journal_covers(rows, native, began, JournalEvidence(journal, receipts | discovery["targets"], attempted)):
        discovery["journal_covered_invocations"] = discovery.pop("unresolved_invocations")
        discovery["unresolved_invocations"] = []
    discovery["targets"].update(journal | receipts)
    return discovery


def check(
    entries: list[dict],
    *,
    owners: Sequence[str] = (),
    reader_for_repo: Callable[[str, float], GitHubReader] | None = None,
    read_timeout: float = 600,
) -> dict:
    deadline = time.time() + read_timeout
    native_sessions = {entry["session_id"] for entry in entries}
    targets: dict[str, set[tuple[str, int]]] = {}
    unresolved, covered = [], []
    expanded = history.expand(entries)
    attempts = {native: claim_log.attempts(native) for native in {entry["session_id"] for entry in expanded}}
    began = lineage_start(expanded)
    for entry in expanded:
        attempted = frozenset(attempt.target for attempt in attempts[entry["session_id"]])
        discovery = recorded_targets(entry, began, attempted)
        targets.setdefault(entry["session_id"], set()).update(discovery["targets"])
        for found, items in ((unresolved, discovery["unresolved_invocations"]), (covered, discovery["journal_covered_invocations"])):
            found.extend({**item, "session": entry["session_id"], "transcript": entry["transcript"]} for item in items)
    running = {native: {attempt.target for attempt in found if attempt.running} for native, found in attempts.items()}
    for native, active in running.items():
        targets.setdefault(native, set()).update(active)
    repos = {repo for records in targets.values() for repo, _ in records}
    accounts = set(owners) | {repo.split("/")[0] for repo in repos}
    released, blocked, readers, pulls_cache, data_cache = [], [], {}, {}, {}
    complete = not unresolved
    for native, records in sorted(targets.items()):
        for repo, number in sorted(records):
            try:
                if (repo, number) in running[native]:
                    raise ValueError("a claim attempt is still running; let it finish, then check again")
                if repo not in readers:
                    readers[repo] = (reader_for_repo or repository_reader)(repo, deadline)
                selected = readers[repo]
                if not selected.expected_actor:
                    raise ValueError("repository's configured automation identity is unknown")
                released.extend(
                    check_record(
                        selected, repo, number, native, selected.expected_actor, pulls_cache=pulls_cache, data_cache=data_cache
                    )
                )
            except (ValueError, OSError, GitHubReadError, GitHubReadShapeError, github_identity.GitHubAppError) as error:
                blocked.append(f"{repo}#{number}: {error}")
                complete = False
    return {
        "operation": CHECK_OPERATION,
        "ok": not blocked and complete,
        "claims_clear": not blocked and complete,
        "session_ids": sorted(native_sessions),
        "coverage": {
            "owners": sorted(accounts),
            "repositories": sorted(repos),
            "complete": complete,
            "scope": "session claim journals and recorded transcript claims",
        },
        "request_count": sum(len(selected.requests) for selected in readers.values()),
        "released_claims": released,
        "blocked": blocked,
        "unresolved_invocations": unresolved,
        "journal_covered_invocations": covered,
    }


def require_own_transcript(entry: dict) -> None:
    identities = set()
    with open(entry["transcript"], encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            identities.add(record.get("sessionId"))
            if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict):
                identities.add(record["payload"].get("id"))
    if entry.get("native_child", entry["session_id"]) not in identities:
        raise ValueError(f"transcript {entry['transcript']} does not belong to session {entry['session_id']}")


def repository_reader(repo: str, deadline: float) -> GitHubReader:
    # The shared reader selects the existing App by target repository, exactly
    # as the claim helper does. No credential/config mutation or fallback.
    return GitHubReader(
        repository=repo,
        expected_actor=github_identity.automation_login(),
        gh_prefix_args=["--require-automation-auth"],
        deadline_at=deadline,
        cache_revalidate=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read back every claim of the named sessions on GitHub.")
    parser.add_argument("--owner", action="append", default=[], help="Account context; never narrows the session claim targets")
    parser.add_argument(
        "--repository", action="append", default=[], help="Repository context; never narrows the session claim targets"
    )
    parser.add_argument("--session", action="append", required=True, help="Native session ID; repeat for child sessions with their own IDs")
    parser.add_argument(
        "--transcript", action="append", required=True, type=Path, help="Native transcript for each --session, in the same order"
    )
    args = parser.parse_args(argv)
    try:
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner) for owner in args.owner):
            raise ValueError("owner must be a GitHub login")
        if any(not sessions.REPO.fullmatch(repo) for repo in args.repository):
            raise ValueError("repository must use ACCOUNT/REPOSITORY form")
        if len(args.session) != len(args.transcript):
            raise ValueError("pass one --transcript for each --session")
        entries = [
            {"session_id": session, "transcript": str(transcript.expanduser().resolve())}
            for session, transcript in zip(args.session, args.transcript, strict=True)
        ]
        for entry in entries:
            require_own_transcript(entry)
        report = check(entries, owners=args.owner)
        report["checked_at"] = time.time()
        print(json.dumps(report, sort_keys=True))
        return 0 if report["claims_clear"] else 1
    except (ValueError, OSError, TypeError, KeyError, GitHubReadError, GitHubReadShapeError, github_identity.GitHubAppError) as error:
        print(json.dumps({"operation": CHECK_OPERATION, "session_ids": args.session,
                          "ok": False, "claims_clear": False, "coverage": {"complete": False},
                          "error": str(error), "next_action": "Preserve ownership, record the blocker, and use conditional/no; do not report safe exit yes."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
