#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5", "tree-sitter==0.26.0", "tree-sitter-bash==0.25.1", "tree-sitter-javascript==0.25.0"]
# ///
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from collections.abc import Set as AbstractSet
from datetime import UTC, datetime
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS / "github/scripts"))
sys.path.insert(0, str(SKILLS / "supervisor/scripts"))
import claim_transcripts as history
import github_identity
import github_plan_claim as claim
import github_plan_claim_log as claim_log
import github_plan_release as release
import github_plan_termination as termination
import release_session_claims as sessions
from github_read import (
    GitHubReader,
    GitHubReadError,
    GitHubReadShapeError,
    path_with_query,
)

CHECK_OPERATION = "work-closeout.claims"
JOURNAL_SHIPPED = datetime(2026, 10, 22, 3, 29, 9, tzinfo=UTC)
ORPHANED_WRITE_SECONDS = 600
SEARCH_INDEX_DELAY_SECONDS = 600
SEARCH_PAGE_SIZE = 100
SEARCH_READER = "search"
SEARCHABLE_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{7,}")
RECORD_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/([1-9]\d*)")


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
        excluded = release.excluded_prs(comments, source, issue_repo=repo) if receipt is None else set()
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
                try:
                    return (release.stamp(source["created_at"]) <= release.stamp(pull.get("created_at"))
                            <= release.stamp(releases[0]["created_at"]))
                except (TypeError, ValueError, KeyError):
                    # Unknown chronology cannot hide an independently linked PR.
                    return True
            def opened_after_release(candidate, claim_ended=releases[0]["created_at"]):
                try:
                    return release.stamp(candidate.get("created_at")) > release.stamp(claim_ended)
                except (TypeError, ValueError, KeyError):
                    return False
            def retained_work(candidate):
                return claim.is_retained_work(candidate, author=actor, issue_repo=repo, issue_number=number, target_repo=repo)
            def later_work_for_another_issue(candidate):
                return opened_after_release(candidate) and not claim.pr_issue_reference(candidate, number, repo=repo, attestation=True)
            pulls = [p for p in pulls if (p.get("head") or {}).get("ref") == record["branch"] and not later_work_for_another_issue(p)
                     or p.get("html_url", "").casefold() not in excluded and retained_work(p)
                     and (p["number"] in named or not claim.is_train_batch(p) and during_claim(p))]
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


def receipt_values(rows: list[dict]) -> list[dict]:
    blocks = [row for row in rows if "claim" in row]
    blocks.extend(block for found in history.tool_outputs(rows).values() for block in found)
    return [value for block in blocks for value in transcript_values(block)]


def unmatched_foreign_receipt(value: dict, native: str, journals: dict[str, dict[tuple[str, int], float]]) -> bool:
    source = value.get("claim")
    if not isinstance(source, dict) or source.get("session") == native:
        return False
    session = source.get("session")
    return not isinstance(session, str) or session not in journals or history.claim_target(value, session) not in journals[session]


def began_after_journal(rows: list[dict], entry: dict) -> bool:
    meta = history.metadata(rows)
    if meta:
        first, identity = meta, meta.get("id") == entry.get("native_child", entry["session_id"])
    else:
        first = next((row for row in rows if "uuid" in row), {})
        identity = first.get("sessionId") == entry["session_id"]
    try:
        began = datetime.fromisoformat(first["timestamp"])
    except (KeyError, TypeError, ValueError):
        return False
    return identity and began.tzinfo is not None and began > JOURNAL_SHIPPED


def recorded_targets(
    entry: dict,
    journal: AbstractSet[tuple[str, int]] | None = None,
    *,
    journals: dict[str, dict[tuple[str, int], float]] | None = None,
) -> dict:
    require_own_transcript(entry)
    native = entry["session_id"]
    rows = entry.get("_transcript_records") or history.read_records(Path(entry["transcript"]))
    values = receipt_values(rows)
    receipts = {target for value in values if (target := history.claim_target(value, native)) is not None}
    return {
        "targets": receipts | (claim_log.read(native) if journal is None else journal),
        "receipts": receipts,
        "journal_era": began_after_journal(rows, entry)
        and not any(unmatched_foreign_receipt(value, native, journals or {}) for value in values),
    }


def reconstructed_targets(entry: dict) -> dict:
    rows = entry.get("_transcript_records") or history.read_records(Path(entry["transcript"]))
    discovery = history.literal_targets(
        rows, entry["session_id"], directory=Path(entry["native_output_directory"]) if entry.get("native_output_directory") else None
    )
    unresolved = discovery["unresolved_invocations"]
    if unresolved:
        inputs = {call.identifier: (call.text, call.javascript, call.name, call.index) for call in history.tool_details(rows)}
        recorded = history.tool_outputs(rows, followups=False)
        ended = {item["call_id"] for item in unresolved if item.get("ended")}
        finished = result_times(rows, ended, inputs, recorded)
        for item in unresolved:
            item["finished_at"] = finished.get(item["call_id"])
    return discovery


def result_times(rows: list[dict], call_ids: set[str], inputs: dict, recorded: dict[str, list[object]]) -> dict[str, float]:
    producers = {call: {call, *history.ordered_polls(call, inputs, recorded)} for call in call_ids}
    latest: dict[str, float] = {}
    for row in rows:
        produced = set(history.tool_outputs([row], followups=False))
        if not produced:
            continue
        try:
            at = release.stamp(row["timestamp"]).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        for call, identifiers in producers.items():
            if produced & identifiers and at >= latest.get(call, at):
                latest[call] = at
    return latest


def search_account() -> str:
    if github_identity.configured_value("GITHUB_CLIENT_APP_OWNERS"):
        raise ValueError("a client App is configured; one search cannot cover its repositories")
    config = github_identity.github_app_config()
    if config is None:
        raise ValueError("the automation App is not configured")
    installations = github_identity.app_installations(config)
    if [str(installation["id"]) for installation in installations] != [config.installation_id]:
        raise ValueError("the App is installed on more than its configured account; one search cannot cover them")
    return installations[0]["account"]["login"]


def session_records(reader: GitHubReader, account: str, session: str) -> set[tuple[str, int]]:
    if not SEARCHABLE_SESSION.fullmatch(session):
        raise ValueError(f"session {session!r} cannot be searched exactly")
    query = f'"{session}" user:{account}'
    body = reader.request("GET", path_with_query("/search/issues", {"q": query, "per_page": SEARCH_PAGE_SIZE}),
                          step="closeout_session_records", bucket="search", fresh=True).body
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list) or body.get("incomplete_results") is not False or body.get("total_count") != len(items):
        raise ValueError(f"search for session {session} returned incomplete results")
    found = set()
    for item in items:
        match = RECORD_URL.fullmatch(str((item or {}).get("html_url", "")))
        if not match or match[1].split("/")[0].casefold() != account.casefold():
            raise GitHubReadShapeError(f"search for session {session} returned a record outside {account}")
        found.add((match[1], int(match[2])))
    return found


def search_sessions(natives: AbstractSet[str], search_reader: Callable[[], GitHubReader]) -> tuple[dict[str, set[tuple[str, int]]], list[str]]:
    if not natives:
        return {}, []
    try:
        account = search_account()
        reader = search_reader()
    except (ValueError, OSError, GitHubReadError, GitHubReadShapeError, github_identity.GitHubAppError) as error:
        return {}, [str(error)]
    found, errors = {}, []
    for native in sorted(natives):
        try:
            found[native] = session_records(reader, account, native)
        except (ValueError, OSError, GitHubReadError, GitHubReadShapeError) as error:
            errors.append(str(error))
    return found, errors


def settle_from_search(
    unresolved: list[dict],
    targets: dict[str, set[tuple[str, int]]],
    released: list[dict],
    check_target: Callable[[str, str, int], None],
    search_reader: Callable[[], GitHubReader],
    journal_covered: AbstractSet[str] = frozenset(),
) -> tuple[list[dict], list[str]]:
    indexed_before = time.time() - SEARCH_INDEX_DELAY_SECONDS
    eligible = [item for item in unresolved if item.get("finished_at") is not None and item["finished_at"] <= indexed_before]
    settling = {item["session"] for item in eligible}
    found, errors = search_sessions(settling | journal_covered, search_reader)
    settled = []
    for native, records in sorted(found.items()):
        known = {(entry["repository"], entry["record"]) for entry in released if entry["source_session"] == native}
        for repo, number in sorted(records - targets.setdefault(native, set())):
            targets[native].add((repo, number))
            check_target(native, repo, number)
        if native not in settling:
            continue
        if not known <= records:
            errors.append(f"search has not indexed every known claim of session {native} yet")
            continue
        settled.extend(item for item in eligible if item["session"] == native)
    return settled, errors


def covered_by_journal(discoveries: list[dict], written: dict[tuple[str, int], float]) -> bool:
    return all(found["journal_era"] and found["receipts"] <= written.keys() for found in discoveries)


def reboot_bounds(entries: list[dict], expanded: list[dict], record: dict | None, *, deadline: float | None = None) -> dict[str, float]:
    if record is None:
        return {}
    if len(entries) != 1 or record.get("session_id") != entries[0]["session_id"]:
        raise ValueError("reboot recovery requires one matching original session")
    if Path(record["transcript"]).resolve() != Path(entries[0]["transcript"]).resolve():
        raise ValueError("reboot recovery transcript differs from the original session")
    proof = termination.verify(record, record["session_id"], deadline=deadline)
    if not proof.get("booted_at"):
        raise ValueError("historical invocation recovery requires verified reboot proof")
    bounds = {}
    natives = {item["transcript"]: item.get("native_child", item["session_id"]) for item in expanded}
    for path, native in sorted(natives.items()):
        child = {**record, "session_id": native, "transcript": path}
        original = native == record["session_id"] and Path(path).resolve() == Path(record["transcript"]).resolve()
        checked = proof if original else termination.verify(child, native, deadline=deadline)
        if checked.get("booted_at") != proof["booted_at"]:
            raise ValueError("native lineage does not share the verified reboot bound")
        bounds[path] = release.stamp(checked["booted_at"]).timestamp()
    return bounds


def live_claim_writers(native: str, targets: AbstractSet[tuple[str, int]]) -> list[str]:
    listing = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,command="], capture_output=True, text=True, check=True).stdout
    processes = {int(pid): (int(parent), command) for pid, parent, command in
                 (line.split(None, 2) for line in listing.splitlines() if len(line.split(None, 2)) == 3)}
    own_lineage, pid = set(), os.getpid()
    while pid in processes and pid not in own_lineage:
        own_lineage.add(pid)
        pid = processes[pid][0]
    posts = [f"/repos/{repo}/issues/{number}/comments" for repo, number in targets]
    return [command for pid, (_, command) in sorted(processes.items())
            if pid not in own_lineage and ("gh-plan" in command and native in command
                                           or "POST" in command and any(post in command for post in posts))]


def running_writes(native: str, written: dict[tuple[str, int], float]) -> list[str]:
    try:
        return [f"claim write still running: {command}" for command in live_claim_writers(native, written.keys())]
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        return [f"cannot list running claim writes: {error}"]


def unsettled_attempts(native: str, written: dict[tuple[str, int], float], running_before: list[str], checked_at: float) -> list[str]:
    reasons = list(dict.fromkeys([*running_before, *running_writes(native, written)]))
    if any(at > checked_at - ORPHANED_WRITE_SECONDS for at in written.values()):
        reasons.append("a claim attempt began less than 10 minutes ago; check again after it settles")
    if claim_log.written_at(native) != written:
        reasons.append("the claim journal changed during this check; check again")
    return reasons


def check(
    entries: list[dict],
    *,
    owners: Sequence[str] = (),
    reader_for_repo: Callable[[str | None, float], GitHubReader] | None = None,
    read_timeout: float = 600,
    termination_record: dict | None = None,
) -> dict:
    deadline = time.time() + read_timeout
    source_deadline = time.monotonic() + read_timeout
    native_sessions = {entry["session_id"] for entry in entries}
    targets: dict[str, set[tuple[str, int]]] = {}
    discoveries: dict[str, list[dict]] = {}
    unresolved = []
    expanded = history.expand(entries)
    bounds = reboot_bounds(entries, expanded, termination_record, deadline=source_deadline)
    written = {native: claim_log.written_at(native) for native in {entry["session_id"] for entry in expanded}}
    for entry in expanded:
        discovery = recorded_targets(entry, written[entry["session_id"]].keys(), journals=written)
        discoveries.setdefault(entry["session_id"], []).append(discovery)
        targets.setdefault(entry["session_id"], set()).update(discovery["targets"])
    journal_covered = {native for native, found in discoveries.items() if covered_by_journal(found, written[native])}
    for entry in expanded:
        if entry["session_id"] in journal_covered:
            continue
        discovery = reconstructed_targets(entry)
        targets[entry["session_id"]].update(discovery["targets"])
        unresolved.extend(
            {**item, "session": entry["session_id"], "transcript": entry["transcript"]}
            for item in discovery["unresolved_invocations"]
        )
    for item in unresolved:
        if item["transcript"] in bounds:
            item["finished_at"] = bounds[item["transcript"]]
    running_before = {native: running_writes(native, written[native]) for native in journal_covered}
    released, blocked, readers, pulls_cache, data_cache = [], [], {}, {}, {}

    def check_target(native: str, repo: str, number: int) -> None:
        try:
            if repo not in readers:
                readers[repo] = (reader_for_repo or repository_reader)(repo, deadline)
            selected = readers[repo]
            if not selected.expected_actor:
                raise ValueError("repository's configured automation identity is unknown")
            released.extend(
                check_record(selected, repo, number, native, selected.expected_actor, pulls_cache=pulls_cache, data_cache=data_cache)
            )
        except (ValueError, OSError, GitHubReadError, GitHubReadShapeError, github_identity.GitHubAppError) as error:
            blocked.append(f"{repo}#{number}: {error}")

    def search_reader() -> GitHubReader:
        readers[SEARCH_READER] = (reader_for_repo or repository_reader)(None, deadline)
        return readers[SEARCH_READER]

    for native_session, records in sorted(targets.items()):
        for target in sorted(records):
            check_target(native_session, *target)
    settled, search_errors = settle_from_search(unresolved, targets, released, check_target, search_reader, journal_covered)
    unresolved = [item for item in unresolved if item not in settled]
    checked_at = time.time()
    for native in sorted(journal_covered):
        blocked.extend(f"session {native}: {reason}" for reason in unsettled_attempts(native, written[native], running_before[native], checked_at))
    if termination_record is not None and reboot_bounds(entries, history.expand(entries), termination_record, deadline=source_deadline) != bounds:
        raise ValueError("native reboot coverage changed during claim readback")
    repos = {repo for records in targets.values() for repo, _ in records}
    accounts = set(owners) | {repo.split("/")[0] for repo in repos}
    complete = not unresolved and not blocked
    return {
        "operation": CHECK_OPERATION,
        "ok": complete,
        "claims_clear": complete,
        "session_ids": sorted(native_sessions),
        "coverage": {
            "owners": sorted(accounts),
            "repositories": sorted(repos),
            "complete": complete,
            "scope": "session claim journals, recorded transcript claims, and the session's claim records found by GitHub search",
            "search_errors": search_errors,
        },
        "request_count": sum(len(selected.requests) for selected in readers.values()),
        "released_claims": released,
        "blocked": blocked,
        "unresolved_invocations": unresolved,
        "settled_invocations": settled,
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


def repository_reader(repo: str | None, deadline: float) -> GitHubReader:
    # The shared reader selects the existing App by target repository, exactly
    # as the claim helper does. No credential/config mutation or fallback.
    return GitHubReader(
        repository=repo,
        expected_actor=github_identity.automation_login(),
        gh_prefix_args=["--require-automation-auth"],
        deadline_at=deadline,
        cache_revalidate=True,
        include_archived=True,
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
    parser.add_argument("--termination-record", type=Path, help="Original Codex source-host record for verified reboot recovery")
    parser.add_argument("--confirm-session-ended", action="store_true", help="Attest native task and unavailable peer closure")
    parser.add_argument("--read-timeout", type=float, default=600, help="Bounded aggregate claim/source read budget in seconds")
    args = parser.parse_args(argv)
    try:
        if not math.isfinite(args.read_timeout) or args.read_timeout <= 0:
            raise ValueError("read timeout must be a positive finite number of seconds")
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
        record_bytes = args.termination_record.read_bytes() if args.termination_record else None
        if record_bytes is not None and not args.confirm_session_ended:
            raise ValueError("reboot recovery requires --confirm-session-ended after native task and peer verification")
        record = None
        if record_bytes is not None:
            import github_plan_native
            record = github_plan_native._cancel_selected(args.termination_record, args.session[0])
        report = check(entries, owners=args.owner, termination_record=record, read_timeout=args.read_timeout)
        if args.termination_record and args.termination_record.read_bytes() != record_bytes:
            raise ValueError("termination record changed during claim readback")
        report["checked_at"] = time.time()
        print(json.dumps(report, sort_keys=True))
        return 0 if report["claims_clear"] else 1
    except (ValueError, OSError, TypeError, KeyError, subprocess.SubprocessError,
            GitHubReadError, GitHubReadShapeError, github_identity.GitHubAppError) as error:
        print(json.dumps({"operation": CHECK_OPERATION, "session_ids": args.session,
                          "ok": False, "claims_clear": False, "coverage": {"complete": False},
                          "error": str(error), "next_action": "Preserve ownership, record the blocker, and use conditional/no; do not report safe exit yes."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
