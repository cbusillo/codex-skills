# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Reconcile planning metadata for GitHub-confirmed keyword closures.

GitHub owns the closing references. A merged prerequisite or Refs is never
completion evidence, and open or contributor-reported issues need review.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import pathlib
import subprocess
from typing import Any

import github_read

QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    pullRequest(number: $number) {
      merged baseRefName url mergeCommit { oid }
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


def reconcile(plan: Any, reader: github_read.GitHubReader, repo: str, number: int, *, apply: bool) -> dict[str, Any]:
    pull, references = closing_references(reader, repo, number)
    outcomes: list[dict[str, Any]] = []
    for issue_repo, issue_number in references:
        entry: dict[str, Any] = {"repo": issue_repo, "number": issue_number}
        outcomes.append(entry)
        # Cross-repository references require their own owning session.
        if issue_repo.casefold() != repo.casefold():
            entry["status"] = "review_cross_repository"
            continue
        _, issue = plan.get_issue(str(issue_number), repo)
        config = plan.load_config(repo)
        labels = plan.normalize_labels(issue.get("labels"))
        planning_labels = {config["labels"][key] for key in ("plan", "active", "blocked", "waiting", "stale", "done")}
        if not planning_labels.intersection(labels) or "pull_request" in issue:
            entry["status"] = "not_planning_issue"
            continue
        if not plan.issue_body_is_fully_managed(issue):
            entry["status"] = "review_reporter_acceptance"
            continue
        if not plan.issue_matches_close_reason(issue, "completed"):
            entry["status"] = "review_open_finish_line"
            continue
        # Use the existing closure's native relationship preflight even in preview.
        plan.close_relationship_preflight(repo, issue_number, "completed")
        if not apply:
            entry["status"] = "would_reconcile"
            continue
        radar = pathlib.Path(__file__).resolve().parents[2] / "github-work-rollup/scripts/github_unanswered_comments.py"
        try:
            subprocess.run(["uv", "run", str(radar), "--thread", f"{repo}#{issue_number}"],
                           capture_output=True, text=True, timeout=120, check=True)
        except subprocess.CalledProcessError:
            entry["status"] = "review_comments_or_coverage"
            continue
        # Never call close on an open issue: recheck just before its metadata path.
        _, fresh = plan.get_issue(str(issue_number), repo)
        if not plan.issue_matches_close_reason(fresh, "completed"):
            raise ValueError("Issue reopened before reconciliation")
        evidence = f"Confirmed default-branch landing: {pull['url']} at {pull['mergeCommit']['oid']}. GitHub lists this issue as a closing reference. Planning metadata reconciled through the existing close preflight; retained artifact custody remains on the source PR/handoff."
        result = captured(plan.cmd_close, argparse.Namespace(
            repo=repo, issue=str(issue_number), planning_checkout=None, reason="completed",
            body=evidence, body_file=None, owner=None, project=None, only_closed=True))
        entry["close"] = result
        if not result.get("ok") or (result.get("project") or {}).get("warning"):
            entry["status"] = "reconciliation_incomplete"
            continue
        status = (f"State: Complete; confirmed default-branch landing in {pull['url']} at {pull['mergeCommit']['oid']}.\n"
                  "Next action: None for this deliverable; parent issues retain their own finish lines.\n"
                  "Blocked by: None.\nWaiting for: None.\n"
                  f"Evidence and retained artifact custody: {pull['url']} and its source handoff.\n")
        entry["section"] = captured(plan.cmd_update_section, argparse.Namespace(
            repo=repo, issue=str(issue_number), section="Current Status", body=status, body_file=None))
        _, verified = plan.get_issue(str(issue_number), repo)
        observed = set(plan.normalize_labels(verified.get("labels")))
        stale = {config["labels"][key] for key in ("active", "blocked", "waiting", "stale")}
        if (not plan.issue_matches_close_reason(verified, "completed") or observed & stale
                or config["labels"]["done"] not in observed
                or plan.issue_plan_sections(verified).get("Current Status", "").strip() != status.strip()):
            raise ValueError("Planning metadata readback did not match")
        entry["status"] = "reconciled"
    return {"ok": all(item["status"] in {"reconciled", "would_reconcile", "not_planning_issue"} for item in outcomes),
            "pr": number, "landing_sha": pull["mergeCommit"]["oid"], "apply": apply, "issues": outcomes}
