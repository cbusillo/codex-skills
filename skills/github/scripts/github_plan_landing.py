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
import shlex
import subprocess
from typing import Any

import github_read
import github_plan_status

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


def linked_issues(claim_module: Any, repo: str, pull: dict[str, Any]) -> list[tuple[str, int]]:
    text = (pull.get("title") or "") + "\n" + (pull.get("body") or "")
    refs = [(match[1] or match[2] or repo, int(match[3])) for match in re.finditer(
        r"(?:https://github[.]com/([\w.-]+/[\w.-]+)/issues/|([\w.-]+/[\w.-]+)?#)([1-9]\d*)\b", text)]
    return [(target, issue) for target, issue in refs if claim_module.pr_issue_reference(
        pull, issue, repo=target, local_references=target.casefold() == repo.casefold())]


def source_references(plan: Any, repo: str, number: int, pull: dict[str, Any]) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """References discover review work; they never attest completion."""
    refs = linked_issues(plan.github_plan_claim, repo, pull)
    timeline = []
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
            timeline.append((ref["repo"], ref["number"]))
    return list(dict.fromkeys(refs)), list(dict.fromkeys(timeline))


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


def unread_wait(error: Exception) -> dict[str, Any]:
    """Keep classified read evidence; a denial never authorizes another write."""
    failure = getattr(error, "failure", None)
    disposition = github_plan_status.failure_disposition(
        failure.cause if failure is not None else None, failure.write_outcome if failure is not None else None,
        writes_started=False)
    if disposition["repository_denied"] or disposition["stop_writes"]:
        raise error
    evidence = {"message": str(error), **getattr(error, "payload", {})}
    if failure is not None:
        evidence["failure"] = failure.as_dict()
    return {"version": 1, "state": "review", "evidence": json.dumps(evidence, sort_keys=True), "waits": []}


def snapshot(plan: Any, repo: str, number: int, *, checkout: str | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    _, issue = plan.get_issue(str(number), repo)
    config = plan.load_config(repo, checkout=checkout) if checkout else plan.load_config(repo)
    if not planning_issue(issue, config):
        return issue, config, {"version": 1, "state": "review", "evidence": "Not a planning issue", "waits": []}
    try:
        _, comments = plan.collect_paged_rest_items(
            f"/repos/{repo}/issues/{number}/comments", query={}, bucket="rest_core", step_prefix="landing_comments")
        wait = plan.current_wait(issue, comments, repo)
    except (ValueError, plan.PlanError) as error:
        wait = unread_wait(error)
    return issue, config, wait


def planning_issue(issue: dict[str, Any], config: dict[str, Any]) -> bool:
    return github_plan_status.is_planning_issue(issue, config["labels"])


def comment_attention(repo: str, number: int) -> bool:
    radar = pathlib.Path(__file__).resolve().parents[2] / "github-work-rollup/scripts/github_unanswered_comments.py"
    try:
        subprocess.run(["uv", "run", str(radar), "--thread", f"{repo}#{number}"],
                       capture_output=True, text=True, timeout=120, check=True)
    except subprocess.CalledProcessError:
        return True
    return False


# https://github.com/cbusillo/codex-skills/issues/2333
RELATIONSHIP_DERIVED_ISSUE_FIELDS = frozenset(
    {"updated_at", "issue_dependencies_summary", "sub_issues_summary", "parent_issue_url"})


def changed_issue_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return sorted(key for key in before.keys() | after.keys()
                  if key not in RELATIONSHIP_DERIVED_ISSUE_FIELDS and before.get(key) != after.get(key))


def reconcile_related(plan: Any, repo: str, number: int, *, apply: bool, entry: dict[str, Any]) -> None:
    discovery: dict[str, Any] = {}
    checkout = entry.get("planning_checkout")
    if checkout:
        plan.load_config(repo, checkout=checkout, discovery=discovery)
    else:
        plan.load_config(repo, discovery=discovery)
    entry["planning_config"] = discovery
    if discovery.get("status") not in {"discovered", "verified_defaults"}:
        if entry.get("config_optional_unless_planning"):
            _, source = plan.get_issue(str(number), repo)
            # Unknown custom configuration stays partial. Only an ordinary
            # generic close may make this configuration review informational.
            # A failed read leaves the explicit source required.
            entry["required"] = github_plan_status.is_planning_issue(
                source, github_plan_status.DEFAULT_STATUS_LABELS)
        entry.update(status="review_planning_config", next_action="Run from a matching target checkout or pass --planning-checkout to reconcile-status so target configuration can be verified")
        return
    issue, config, wait = snapshot(plan, repo, number, checkout=checkout)
    if not planning_issue(issue, config):
        entry["status"] = "not_planning_issue"
        return
    if issue.get("state") not in {"open", "closed"}:
        entry["status"] = "review_related_finish_line"
        return
    _, relationships = plan.read_issue_relationships(repo, number, include_blocking=False)
    derived = github_plan_status.derive(issue, relationships, wait, label_names=config["labels"])
    blockers = derived["blocked_by"]
    key = derived["status"]
    entry.update(derived_status=key, wait=wait, open_blockers=blockers,
                 blocked_by=blockers, derived=derived,
                 finish_line="preserved; related landing does not prove completion")
    if not derived["labels_writable"]:
        entry.update(**github_plan_status.projection_outcome(derived, "preserved"),
                     next_action="Review the missing wait evidence" if derived["wait_review_required"] else "Preserve the deliberate scope hold")
        return
    names = set(plan.normalize_labels(issue.get("labels")))
    changes = github_plan_status.label_changes(sorted(names), config["labels"], derived)
    wanted = {config["labels"][key] for key in derived["labels"]}
    entry["derived_labels"] = sorted(wanted)
    remove = changes["remove"]
    if not changes["add"] and not remove:
        entry.update(github_plan_status.projection_outcome(derived, "unchanged"))
        return
    if not apply:
        entry.update(github_plan_status.projection_outcome(derived, "would_reconcile"))
        return
    if getattr(plan, "planning_comment_attention", comment_attention)(repo, number):
        entry["status"] = "review_comments_or_coverage"
        return
    # Re-run the observation immediately before a label write; never edit wait records or prose.
    fresh, fresh_config, fresh_wait = snapshot(plan, repo, number, checkout=checkout)
    _, fresh_relationships = plan.read_issue_relationships(repo, number, include_blocking=False)
    changed = changed_issue_fields(issue, fresh)
    if not github_plan_status.same_wait_projection(issue, fresh_wait, wait):
        changed.append("wait")
    if fresh_relationships != relationships:
        changed.append("relationships")
    if fresh_config != config:
        changed.append("planning_config")
    if changed:
        raise ValueError("Related planning state changed before reconciliation: " + ", ".join(changed))
    _, gh_cmd, expected = plan.comment_route()
    entry["writes_started"] = True
    try:
        entry["labels"] = plan.github_issue_core.edit_issue(
            number, repo=repo, add_labels=changes["add"] or None, remove_labels=remove or None,
            gh_cmd=gh_cmd, expected_actor=expected, operation=plan.CURRENT_OPERATION)
    except plan.github_issue_core.IssueError as exc:
        raise plan.plan_error_from_issue(exc) from exc
    verified, _, verified_wait = snapshot(plan, repo, number, checkout=checkout)
    _, verified_relationships = plan.read_issue_relationships(repo, number, include_blocking=False)
    expected_names = set(changes["result"])
    if (set(plan.normalize_labels(verified.get("labels"))) != expected_names
            or verified.get("state") != issue["state"] or verified.get("body") != issue.get("body")
            or not github_plan_status.same_wait_projection(issue, verified_wait, wait) or verified_relationships != relationships):
        raise ValueError("Related planning readback did not match")
    entry.update(github_plan_status.projection_outcome(derived, "reconciled"))


def reconciliation_error(entry: dict[str, Any], error: Exception) -> tuple[bool, bool]:
    """Return (repository denied, stop writes), preserving classified evidence."""
    failure = getattr(error, "failure", None)
    entry.update(status="reconciliation_failed",
                 error={"message": str(error), **getattr(error, "payload", {})})
    if failure is not None:
        entry["error"]["failure"] = failure.as_dict()
    disposition = github_plan_status.failure_disposition(
        failure.cause if failure is not None else None, failure.write_outcome if failure is not None else None,
        writes_started=bool(entry.get("writes_started")), validation_error=isinstance(error, ValueError))
    entry["status"] = disposition["status"]
    if disposition["repository_denied"]:
        entry["next_action"] = "Existing target authority must reconcile; do not retry with another identity or grant"
    return disposition["repository_denied"], disposition["stop_writes"]


def run_reconciliation_tasks(plan: Any, tasks: list[dict[str, Any]], *, apply: bool,
                             pull: dict[str, Any] | None = None,
                             denied_repositories: dict[str, Any] | None = None,
                             stopped_reason: dict[str, Any] | None = None) -> dict[str, Any]:
    """One partial-success/denial contract for close and landing adapters."""
    stopped = stopped_reason
    denied: dict[str, Any] = dict(denied_repositories or {})
    for entry in tasks:
        if "status" in entry:
            continue
        repo, number = entry["repo"], entry["number"]
        if stopped is not None:
            entry.update(status="not_attempted", reason=stopped)
            continue
        if repo.casefold() in denied:
            entry.update(status="not_attempted", reason=denied[repo.casefold()], informational=True,
                         next_action="Existing target authority must reconcile; this session will not repeat the denied write")
            continue
        if apply and entry.get("automatic") and not plan.automatic_planning_writes_allowed():
            entry.update(status="review_posting_authority",
                         reason="Exact personal command approval does not authorize automatic planning writes")
            continue
        try:
            if entry["role"] == "source":
                assert pull is not None
                reconcile_issue(plan, repo, number, pull, apply=apply, entry=entry)
            else:
                reconcile_related(plan, repo, number, apply=apply, entry=entry)
        except (plan.PlanError, ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
            repo_denied, stop = reconciliation_error(entry, error)
            reason = {"repo": repo, "number": number, "error": entry["error"]}
            if repo_denied:
                denied[repo.casefold()] = reason
            if stop:
                stopped = reason
    for entry in tasks:
        repo, number = entry["repo"], entry["number"]
        optional_source = entry["role"] == "source" and not entry.get("closing_reference", True) and entry["status"] in {
                "review_open_finish_line", "review_cross_repository", "review_reporter_acceptance", "review_reference_only"}
        entry.update(github_plan_status.classify_outcome(
            entry["status"], verified=bool(entry.get("verified")),
            projection_status=entry.get("projection_status"), completion_required=entry["role"] == "source",
            required=bool(entry.get("required")) or (entry["role"] == "source" and not optional_source)))
        if entry["status"].startswith(("review_", "reconciliation_", "not_attempted")):
            recovery_argv = ["gh-plan.py", "--repo", repo, "reconcile-status", str(number), "--apply"]
            if entry.get("phase") == "neighborhood_discovery":
                recovery_argv.append("--include-dependents")
            if entry.get("planning_checkout"):
                recovery_argv.append("--planning-checkout=" + entry["planning_checkout"])
            entry["recovery"] = {
                "argv": recovery_argv,
                "issue": {"repo": repo, "number": number},
                "next_action": shlex.join(recovery_argv),
                "authority": "Existing authorized target session; preserve any recorded denial"}
    return {**github_plan_status.receipt_summary(tasks), "issues": tasks}


def discovery_failure(plan: Any, repo: str, number: int, error: Exception, *,
                      planning_checkout: str | None = None) -> dict[str, Any]:
    """Retain the explicit root when its native neighborhood cannot be read."""
    entry: dict[str, Any] = {"repo": repo, "number": number, "role": "related", "required": True,
                             "phase": "neighborhood_discovery"}
    if planning_checkout:
        entry["planning_checkout"] = planning_checkout
    reconciliation_error(entry, error)
    return run_reconciliation_tasks(plan, [entry], apply=False)


def reconcile_dependents(plan: Any, repo: str, number: int, *, apply: bool,
                         planning_checkout: str | None = None,
                         denied_repositories: dict[str, Any] | None = None,
                         stopped_reason: dict[str, Any] | None = None) -> dict[str, Any]:
    """The same direct native neighborhood refresh for issue closes and landings."""
    tasks = []
    for target_repo, target_number in neighborhood(plan, repo, [(repo, number)]):
        entry = {"repo": target_repo, "number": target_number, "role": "related", "automatic": True}
        if planning_checkout and target_repo.casefold() == repo.casefold():
            entry["planning_checkout"] = planning_checkout
        if target_repo.split("/")[0].casefold() != repo.split("/")[0].casefold():
            entry.update(status="review_cross_repository", informational=True)
        tasks.append(entry)
    return run_reconciliation_tasks(plan, tasks, apply=apply, denied_repositories=denied_repositories,
                                    stopped_reason=stopped_reason)


def reconcile(plan: Any, reader: github_read.GitHubReader, repo: str, number: int, *, apply: bool) -> dict[str, Any]:
    pull, references = closing_references(reader, repo, number)
    explicit, timeline = source_references(plan, repo, number, pull)
    roots = list(dict.fromkeys([*references, *explicit]))
    sources = list(dict.fromkeys([*roots, *timeline]))
    related = neighborhood(plan, repo, roots)
    tasks = []
    roles: list[tuple[tuple[str, int], bool]] = [(key, True) for key in sources if key in roots or key not in related]
    roles.extend((key, False) for key in related)
    for (issue_repo, issue_number), is_source in roles:
        entry = {"repo": issue_repo, "number": issue_number,
                 "automatic": True,
                 "role": "source" if is_source else "related",
                 "closing_reference": (issue_repo, issue_number) in references}
        if (issue_repo, issue_number) not in roots and (issue_repo, issue_number) not in related:
            entry.update(status="review_reference_only", informational=True,
                         next_action="Timeline mention is context only; owning agent reviews its relevance")
        elif (is_source and issue_repo.casefold() != repo.casefold()
                or issue_repo.split("/")[0].casefold() != repo.split("/")[0].casefold()):
            entry.update(status="review_cross_repository", informational=not entry["closing_reference"])
        tasks.append(entry)
    return {**run_reconciliation_tasks(plan, tasks, apply=apply, pull=pull),
            "pr": number, "landing_sha": pull["mergeCommit"]["oid"], "apply": apply}


def reconcile_issue(plan: Any, repo: str, issue_number: int, pull: dict[str, Any], *,
                    apply: bool, entry: dict[str, Any]) -> None:
    issue, config, wait = snapshot(plan, repo, issue_number)
    if not planning_issue(issue, config):
        entry["status"] = "not_planning_issue"
        return
    if not plan.issue_body_is_fully_managed(issue):
        entry["status"] = "review_reporter_acceptance"
        return
    if not plan.issue_matches_close_reason(issue, "completed"):
        entry["status"] = "review_open_finish_line"
        entry["next_action"] = "Review the full finish line and discussion; use gh-plan.py close only when complete, then rerun reconcile-landing. Refs and merged prerequisites do not prove completion."
        return
    try:
        github_plan_status.wait_records.require_clear(wait)
    except ValueError:
        # Closure is already confirmed. Repair only its lifecycle label view;
        # a held/unread wait does not authorize a completion note or body edit.
        reconcile_related(plan, repo, issue_number, apply=apply, entry=entry)
        entry.update(wait_review_required=True, wait=wait,
                     completion_review="Preserve the wait; owning agent reviews full completion metadata")
        if entry["status"] in {"reconciled", "unchanged", "would_reconcile"}:
            entry.update(projection_status=entry["status"], status="review_wait_record", verified=False)
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
        body=evidence, body_file=None, owner=None, project=None, only_closed=True,
        reconcile_dependents=False))
    entry["close"] = result
    if not result.get("ok") or (result.get("project") or {}).get("warning"):
        entry["status"] = "reconciliation_incomplete"
        return
    status = plan.issue_plan_sections(issue).get("Current Status", "").strip()
    complete = "State: Complete; issue already closed as completed."
    if re.search(r"(?m)^State:", status):
        status = re.sub(r"(?m)^State:.*$", complete, status, count=1)
    else:
        status = complete + "\n" + status
    receipt = f"Landing reconciliation: {pull['url']} at {pull['mergeCommit']['oid']}. Original evidence and retained artifact custody above are preserved."
    if receipt not in status.splitlines():
        status = status.rstrip() + "\n" + receipt + "\n"
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
