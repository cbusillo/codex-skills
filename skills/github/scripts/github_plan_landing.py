# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Reconcile a landing's source records and native planning neighborhood.

GitHub owns the closing references. A merged prerequisite or Refs is never
completion evidence, and open or contributor-reported issues need review.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import pathlib
import re
import subprocess
from typing import Any

import github_read

QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    pullRequest(number: $number) {
      merged baseRefName url title body mergeCommit { oid }
      closingIssuesReferences(first: 100, after: $after) {
        nodes { number repository { nameWithOwner } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


def closing_references(reader: github_read.GitHubReader, repo: str, number: int) -> tuple[dict[str, Any], list[tuple[str, int]]]:
    owner, name = repo.split("/", 1)
    after = None
    references: list[tuple[str, int]] = []
    identity = None
    while True:
        result = reader.graphql_json(QUERY, {"owner": owner, "name": name, "number": number, "after": after},
                                     step="closing_issue_references")
        if not result.ok:
            raise ValueError("Closing-reference read unavailable")
        body = result.body
        if not isinstance(body, dict) or body.get("errors"):
            raise ValueError("Closing-reference GraphQL read incomplete")
        repository = (body.get("data") or {}).get("repository")
        if not isinstance(repository, dict):
            raise TypeError("Repository read unavailable")
        pull = repository.get("pullRequest")
        if not isinstance(pull, dict) or pull.get("merged") is not True:
            raise ValueError("PR is not confirmed merged")
        branch = repository.get("defaultBranchRef") or {}
        sha = (pull.get("mergeCommit") or {}).get("oid")
        if not branch.get("name") or pull.get("baseRefName") != branch["name"] or not sha:
            raise ValueError("PR has no confirmed default-branch landing")
        current = (sha, pull.get("url"), pull["baseRefName"])
        if identity is not None and identity != current:
            raise ValueError("Landing changed during pagination")
        identity = current
        connection = pull.get("closingIssuesReferences")
        if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
            raise TypeError("Closing references missing")
        for node in connection["nodes"]:
            issue_repo = (node.get("repository") or {}).get("nameWithOwner")
            issue_number = node.get("number")
            if not isinstance(issue_repo, str) or not isinstance(issue_number, int):
                raise TypeError("Invalid closing reference")
            references.append((issue_repo, issue_number))
        page = connection.get("pageInfo") or {}
        if page.get("hasNextPage") is False:
            return pull, list(dict.fromkeys(references))
        cursor = page.get("endCursor")
        if page.get("hasNextPage") is not True or not isinstance(cursor, str) or cursor == after:
            raise ValueError("Closing references truncated")
        after = cursor


def captured(command: Any, args: argparse.Namespace) -> dict[str, Any]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        command(args)
    return json.loads(output.getvalue())


def source_references(plan: Any, repo: str, number: int, pull: dict[str, Any]) -> list[tuple[str, int]]:
    """References discover review work; they never attest completion."""
    text = (pull.get("title") or "") + "\n" + (pull.get("body") or "")
    refs = [(match[1] or match[2] or repo, int(match[3])) for match in re.finditer(
        r"(?:https://github[.]com/([\w.-]+/[\w.-]+)/issues/|([\w.-]+/[\w.-]+)?#)([1-9]\d*)\b", text)]
    refs = [(target, issue) for target, issue in refs if plan.github_plan_claim.pr_issue_reference(
        pull, issue, repo=target, local_references=target.casefold() == repo.casefold())]
    _, events = plan.collect_paged_rest_items(
        f"/repos/{repo}/issues/{number}/timeline", query={}, bucket="rest_core", step_prefix="landing_timeline")
    for event in events:
        if event.get("event") != "cross-referenced":
            continue
        issue = (event.get("source") or {}).get("issue")
        if not isinstance(issue, dict):
            raise ValueError("Cross-reference source unavailable")
        if "pull_request" not in issue:
            ref = plan.compact_relationship_issue(issue, "cross_reference")
            refs.append((ref["repo"], ref["number"]))
    return list(dict.fromkeys(refs))


def neighborhood(plan: Any, landing_repo: str, sources: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Read only direct parents/dependents; do not fan out through milestone graphs."""
    seen: set[tuple[str, int]] = set()
    related: list[tuple[str, int]] = []
    if len(sources) > 100:
        raise ValueError("Landing sources exceed 100 issues; coverage incomplete")
    for repo, number in sources:
        if repo.split("/")[0].casefold() != landing_repo.split("/")[0].casefold():
            continue
        _, issue = plan.get_issue(str(number), repo)
        if "pull_request" in issue:
            continue
        _, relationships = plan.read_issue_relationships(repo, number, include_blocking=True)
        parent = plan.read_next_parent(repo, number)
        targets = [*relationships["blocking"], *([parent] if parent else [])]
        for target in targets:
            key = (target["repo"], target["number"])
            if key in seen:
                continue
            if len(set(sources) | seen | {key}) > 100:
                raise ValueError("Landing neighborhood exceeds 100 issues; coverage incomplete")
            seen.add(key)
            related.append(key)
    return related


def snapshot(plan: Any, repo: str, number: int) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    _, issue = plan.get_issue(str(number), repo)
    _, comments = plan.collect_paged_rest_items(
        f"/repos/{repo}/issues/{number}/comments", query={}, bucket="rest_core", step_prefix="landing_comments")
    wait = plan.current_wait(issue, comments, repo)
    return issue, plan.load_config(repo), wait


def planning_issue(plan: Any, issue: dict[str, Any], config: dict[str, Any]) -> bool:
    labels = set(plan.normalize_labels(issue.get("labels")))
    planning = {config["labels"][key] for key in ("plan", "active", "blocked", "waiting", "stale", "done")}
    return "pull_request" not in issue and bool(labels.intersection(planning))


def comment_attention(repo: str, number: int) -> bool:
    radar = pathlib.Path(__file__).resolve().parents[2] / "github-work-rollup/scripts/github_unanswered_comments.py"
    try:
        subprocess.run(["uv", "run", str(radar), "--thread", f"{repo}#{number}"],
                       capture_output=True, text=True, timeout=120, check=True)
    except subprocess.CalledProcessError:
        return True
    return False


def reconcile_related(plan: Any, repo: str, number: int, *, apply: bool, entry: dict[str, Any]) -> None:
    discovery: dict[str, Any] = {}
    plan.load_config(repo, discovery=discovery)
    entry["planning_config"] = discovery
    if discovery.get("status") not in {"discovered", "verified_defaults"}:
        entry.update(status="review_planning_config", next_action="Run from a matching target checkout so its planning configuration can be verified")
        return
    issue, config, wait = snapshot(plan, repo, number)
    if not planning_issue(plan, issue, config):
        entry["status"] = "not_planning_issue"
        return
    if not plan.issue_body_is_fully_managed(issue):
        entry["status"] = "review_reporter_acceptance"
        return
    if issue.get("state") != "open":
        entry["status"] = "review_related_finish_line"
        return
    if wait["state"] == "review":
        entry["status"] = "review_wait_record"
        return
    _, relationships = plan.read_issue_relationships(repo, number, include_blocking=False)
    blockers = [item for item in relationships["blocked_by"] if item["state"] == "open"]
    key = "blocked" if blockers else "waiting" if wait["state"] == "waiting" else "active"
    entry.update(derived_status=key, wait=wait, open_blockers=blockers,
                 finish_line="preserved; related landing does not prove completion",
                 current_status_review="Body preserved; review any prose naming the old blocker against these current machine observations")
    names = set(plan.normalize_labels(issue.get("labels")))
    # Stale/done on an open issue needs scope review, not automatic activation.
    if names.intersection(config["labels"][key] for key in ("stale", "done")):
        entry["status"] = "review_related_finish_line"
        return
    wanted = config["labels"][key]
    remove = [config["labels"][key] for key in ("active", "blocked", "waiting")
              if config["labels"][key] != wanted and config["labels"][key] in names]
    if wanted in names and not remove:
        entry["status"] = "unchanged"
        return
    if not apply:
        entry["status"] = "would_reconcile"
        return
    if comment_attention(repo, number):
        entry["status"] = "review_comments_or_coverage"
        return
    # Re-run the observation immediately before a label write; never edit wait records or prose.
    fresh, fresh_config, fresh_wait = snapshot(plan, repo, number)
    _, fresh_relationships = plan.read_issue_relationships(repo, number, include_blocking=False)
    if fresh != issue or fresh_wait != wait or fresh_relationships != relationships or fresh_config != config:
        raise ValueError("Related planning state changed before reconciliation")
    _, gh_cmd, expected = plan.comment_route()
    entry["writes_started"] = True
    entry["labels"] = plan.github_issue_core.edit_issue(
        number, repo=repo, add_labels=[wanted] if wanted not in names else None, remove_labels=remove or None,
        gh_cmd=gh_cmd, expected_actor=expected, operation=plan.CURRENT_OPERATION)
    verified, _, verified_wait = snapshot(plan, repo, number)
    _, verified_relationships = plan.read_issue_relationships(repo, number, include_blocking=False)
    expected_names = (names - set(remove)) | {wanted}
    if (set(plan.normalize_labels(verified.get("labels"))) != expected_names
            or verified.get("state") != "open" or verified.get("body") != issue.get("body")
            or verified_wait != wait or verified_relationships != relationships):
        raise ValueError("Related planning readback did not match")
    entry["status"] = "reconciled"


def reconcile(plan: Any, reader: github_read.GitHubReader, repo: str, number: int, *, apply: bool) -> dict[str, Any]:
    pull, references = closing_references(reader, repo, number)
    sources = list(dict.fromkeys([*references, *source_references(plan, repo, number, pull)]))
    related = neighborhood(plan, repo, sources)
    outcomes: list[dict[str, Any]] = []
    for issue_repo, issue_number in dict.fromkeys([*sources, *related]):
        # A timeline reference may also be a native dependent. Reconcile its
        # labels as a dependent unless GitHub explicitly lists a closing link.
        is_source = (issue_repo, issue_number) not in related or (issue_repo, issue_number) in references
        entry: dict[str, Any] = {"repo": issue_repo, "number": issue_number,
                                 "role": "source" if is_source else "related",
                                 "closing_reference": (issue_repo, issue_number) in references}
        outcomes.append(entry)
        # Cross-repository references require their own owning session.
        if (is_source and issue_repo.casefold() != repo.casefold()
                or issue_repo.split("/")[0].casefold() != repo.split("/")[0].casefold()):
            entry["status"] = "review_cross_repository"
            entry["informational"] = not entry["closing_reference"]
            continue
        try:
            if is_source:
                reconcile_issue(plan, issue_repo, issue_number, pull, apply=apply, entry=entry)
            else:
                reconcile_related(plan, issue_repo, issue_number, apply=apply, entry=entry)
        except (plan.PlanError, ValueError, TypeError, OSError, subprocess.SubprocessError) as error:
            entry["status"] = "reconciliation_failed"
            entry["error"] = {"message": str(error), **getattr(error, "payload", {})}
        # Informational open references need an owning-agent review but are
        # not failed metadata writes. Closing links and related drift still fail.
        if is_source and not entry["closing_reference"] and entry["status"] in {
                "review_open_finish_line", "review_cross_repository", "review_reporter_acceptance"}:
            entry["informational"] = True
    return {"ok": all(item.get("informational") or item["status"] in {"reconciled", "would_reconcile", "not_planning_issue", "unchanged"} for item in outcomes),
            "coverage": "complete", "review_required": any(item["status"].startswith(("review_", "reconciliation_")) for item in outcomes),
            "pr": number, "landing_sha": pull["mergeCommit"]["oid"], "apply": apply, "issues": outcomes}


def reconcile_issue(plan: Any, repo: str, issue_number: int, pull: dict[str, Any], *,
                    apply: bool, entry: dict[str, Any]) -> None:
    issue, config, wait = snapshot(plan, repo, issue_number)
    if not planning_issue(plan, issue, config):
        entry["status"] = "not_planning_issue"
        return
    if not plan.issue_body_is_fully_managed(issue):
        entry["status"] = "review_reporter_acceptance"
        return
    if not plan.issue_matches_close_reason(issue, "completed"):
        entry["status"] = "review_open_finish_line"
        entry["next_action"] = "Review the full finish line and discussion; use gh-plan.py close only when complete, then rerun reconcile-landing. Refs and merged prerequisites do not prove completion."
        return
    if wait["state"] != "clear":
        entry.update(status="review_wait_record", wait=wait)
        return
    # Use the existing closure's native relationship preflight even in preview.
    plan.close_relationship_preflight(repo, issue_number, "completed")
    if not apply:
        entry["status"] = "would_reconcile"
        return
    if comment_attention(repo, issue_number):
        entry["status"] = "review_comments_or_coverage"
        return
    # Never call close on an open issue: recheck just before its metadata path.
    fresh, fresh_config, fresh_wait = snapshot(plan, repo, issue_number)
    if fresh != issue or fresh_wait != wait or fresh_config != config:
        raise ValueError("Issue changed before reconciliation")
    closing = entry.get("closing_reference", True)
    evidence = (f"Confirmed default-branch landing: {pull['url']} at {pull['mergeCommit']['oid']}. This issue is already closed as completed; a reference alone does not prove its finish line. Planning metadata reconciled through the existing close preflight; retained artifact custody remains on the source PR/handoff."
                if closing else "")
    entry["writes_started"] = True
    result = captured(plan.cmd_close, argparse.Namespace(
        repo=repo, issue=str(issue_number), planning_checkout=None, reason="completed",
        body=evidence, body_file=None, owner=None, project=None, only_closed=True))
    entry["close"] = result
    if not result.get("ok") or (result.get("project") or {}).get("warning"):
        entry["status"] = "reconciliation_incomplete"
        return
    status = (f"State: Complete; issue already closed as completed. Related default-branch landing: {pull['url']} at {pull['mergeCommit']['oid']}.\n"
              "Next action: None for this deliverable; parent issues retain their own finish lines.\n"
              "Blocked by: None.\nWaiting for: None.\n"
              f"Evidence and retained artifact custody: {pull['url']} and its source handoff.\n")
    if closing:
        entry["section"] = captured(plan.cmd_update_section, argparse.Namespace(
            repo=repo, issue=str(issue_number), section="Current Status", body=status, body_file=None))
    else:
        entry["current_status_preserved"] = True
    verified, _, verified_wait = snapshot(plan, repo, issue_number)
    observed = set(plan.normalize_labels(verified.get("labels")))
    stale = {config["labels"][key] for key in ("active", "blocked", "waiting", "stale")}
    if (not plan.issue_matches_close_reason(verified, "completed") or observed & stale
            or config["labels"]["done"] not in observed
            or (closing and plan.issue_plan_sections(verified).get("Current Status", "").strip() != status.strip())
            or (not closing and verified.get("body") != issue.get("body"))
            or verified_wait != wait):
        raise ValueError("Planning metadata readback did not match")
    entry["status"] = "reconciled"
