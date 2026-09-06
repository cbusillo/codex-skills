#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Watch GitHub PR CI and review activity for PR babysitting workflows."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_GH = SCRIPT_DIR.parent.parent / "github" / "scripts" / "gh-with-env-token"
GH_COMMAND = os.environ.get("GH_PR_WATCH_GH") or str(DEFAULT_GH)
DEFAULT_PR_HELPER = SCRIPT_DIR.parent.parent / "github" / "scripts" / "gh-pr.py"
PR_HELPER = os.environ.get("GH_PR_WATCH_PR_HELPER") or str(DEFAULT_PR_HELPER)
IDENTITY_SCRIPT_DIR = SCRIPT_DIR.parent.parent / "github" / "scripts"
if str(IDENTITY_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(IDENTITY_SCRIPT_DIR))
import github_api
import github_identity

FAILED_RUN_CONCLUSIONS = {
    "failure",
    "timed_out",
    "cancelled",
    "action_required",
    "startup_failure",
    "stale",
}
REVIEW_BOT_LOGIN_KEYWORDS = {
    "codex",
}


def configured_bot_logins() -> frozenset[str]:
    automation_login = github_identity.automation_login()
    return frozenset(
        login.casefold()
        for login in (
            *github_identity.configured_bot_logins(),
            *([automation_login] if automation_login else []),
        )
    )


MERGE_BLOCKING_REVIEW_DECISIONS = {
    "REVIEW_REQUIRED",
    "CHANGES_REQUESTED",
}
MERGE_CONFLICT_OR_BLOCKING_STATES = {
    "BLOCKED",
    "DIRTY",
    "DRAFT",
    "UNKNOWN",
}
KNOWN_MERGE_STATES = {
    "BEHIND",
    "BLOCKED",
    "CLEAN",
    "DIRTY",
    "DRAFT",
    "HAS_HOOKS",
    "UNKNOWN",
    "UNSTABLE",
}


class GhCommandError(RuntimeError):
    pass


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Normalize PR/CI/review state for PR babysitting and optionally "
            "trigger flaky reruns."
        )
    )
    parser.add_argument("--pr", default="auto", help="auto, PR number, or PR URL")
    parser.add_argument("--repo", help="Optional OWNER/REPO override")
    parser.add_argument("--poll-seconds", type=int, default=30, help="Watch poll interval")
    parser.add_argument(
        "--max-flaky-retries",
        type=int,
        default=3,
        help="Max rerun cycles per head SHA before stop recommendation",
    )
    parser.add_argument("--state-file", help="Path to state JSON file")
    parser.add_argument("--once", action="store_true", help="Emit one snapshot and exit")
    parser.add_argument("--watch", action="store_true", help="Continuously emit JSONL snapshots")
    parser.add_argument(
        "--retry-failed-now",
        action="store_true",
        help="Rerun failed jobs for current failed workflow runs when policy allows",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable output (default behavior for --once and --retry-failed-now)",
    )
    args = parser.parse_args()

    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be > 0")
    if args.max_flaky_retries < 0:
        parser.error("--max-flaky-retries must be >= 0")
    if args.watch and args.retry_failed_now:
        parser.error("--watch cannot be combined with --retry-failed-now")
    if not args.once and not args.watch and not args.retry_failed_now:
        args.once = True
    return args


def _format_gh_error(cmd, err):
    stdout = (err.stdout or "").strip()
    stderr = (err.stderr or "").strip()
    parts = [f"GitHub CLI command failed: {' '.join(cmd)}"]
    if stdout:
        parts.append(f"stdout: {stdout}")
    if stderr:
        parts.append(f"stderr: {stderr}")
    return "\n".join(parts)


def gh_text(args, repo=None):
    cmd = [GH_COMMAND]
    # `gh api` does not accept `-R/--repo` on all gh versions. The watcher's
    # API calls use explicit endpoints (e.g. repos/{owner}/{repo}/...), so the
    # repo flag is unnecessary there.
    if repo and (not args or args[0] != "api"):
        cmd.extend(["-R", repo])
    cmd.extend(args)
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as err:
        raise GhCommandError("`gh` command not found") from err
    except subprocess.CalledProcessError as err:
        raise GhCommandError(_format_gh_error(cmd, err)) from err
    return proc.stdout


def gh_json(args, repo=None):
    raw = gh_text(args, repo=repo).strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as err:
        raise GhCommandError(f"Failed to parse JSON from gh output for {' '.join(args)}") from err


def parse_pr_spec(pr_spec):
    if pr_spec == "auto":
        return {"mode": "auto", "value": None}
    if re.fullmatch(r"\d+", pr_spec):
        return {"mode": "number", "value": pr_spec}
    parsed = urlparse(pr_spec)
    if parsed.scheme and parsed.netloc and "/pull/" in parsed.path:
        return {"mode": "url", "value": pr_spec}
    raise ValueError("--pr must be 'auto', a PR number, or a PR URL")


def compact_helper_diagnostic(payload, returncode):
    keys = (
        "transport",
        "bucket",
        "actor",
        "expected_actor",
        "status",
        "request_id",
        "attempts",
        "retryable",
        "retry_at",
        "retry_after",
        "retry_exhausted_reason",
        "failed_step",
        "completed_steps",
    )
    diagnostic = {key: payload[key] for key in keys if payload.get(key) is not None}
    diagnostic["ok"] = payload.get("ok") is True
    diagnostic["returncode"] = returncode
    return diagnostic


def pr_helper_json(command, pr_spec=None, repo=None, allow_partial=False):
    helper_path = Path(PR_HELPER)
    if not helper_path.is_file():
        raise GhCommandError(f"REST-first PR helper not found: {PR_HELPER}")
    cmd = [sys.executable, str(helper_path)]
    if repo:
        cmd.extend(["--repo", repo])
    cmd.append(command)
    if pr_spec and pr_spec != "auto":
        cmd.append(pr_spec)
    env = os.environ.copy()
    env["GH_PR_GH"] = GH_COMMAND
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, env=env)

    raw = proc.stdout.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as err:
        detail = github_api.redact_string(proc.stderr.strip())[:500]
        suffix = f": {detail}" if detail else ""
        raise GhCommandError(
            f"REST-first PR helper returned invalid JSON for {command}{suffix}"
        ) from err
    if not isinstance(payload, dict):
        raise GhCommandError(f"Unexpected REST-first PR helper payload for {command}")
    payload["_watcher_diagnostic"] = compact_helper_diagnostic(payload, proc.returncode)
    if proc.returncode != 0 and not allow_partial:
        detail = payload.get("error") or payload.get("recommended_next_action")
        suffix = f": {detail}" if detail else ""
        raise GhCommandError(f"REST-first PR helper failed for {command}{suffix}")
    return payload


def normalize_mergeable(value):
    if value is True:
        return "MERGEABLE"
    if value is False:
        return "CONFLICTING"
    return "UNKNOWN"


def resolve_pr(pr_spec, repo_override=None):
    parsed = parse_pr_spec(pr_spec)
    requested_repo = None
    requested_number = None
    helper_pr_spec = pr_spec
    if parsed["mode"] == "url":
        requested_repo = extract_repo_from_pr_url(pr_spec)
        requested_number = extract_pr_number_from_url(pr_spec)
        if not requested_repo or requested_number is None:
            raise GhCommandError(
                f"Unable to determine repository and PR number from URL: {pr_spec}"
            )
        helper_pr_spec = str(requested_number)
    elif parsed["mode"] == "number":
        requested_number = int(parsed["value"])
    if repo_override and requested_repo and repo_override.casefold() != requested_repo.casefold():
        raise GhCommandError(
            f"PR URL repository {requested_repo} does not match --repo {repo_override}"
        )
    payload = pr_helper_json(
        "view",
        pr_spec=helper_pr_spec,
        repo=repo_override or requested_repo,
    )
    data = payload.get("pr")
    if payload.get("ok") is not True or not isinstance(data, dict):
        raise GhCommandError("REST-first PR helper did not return usable PR metadata")

    pr_url = str(data.get("url") or "")
    url_repo = extract_repo_from_pr_url(pr_url)
    repo = str(payload.get("repo") or url_repo or "")
    if not pr_url or not url_repo or not repo:
        raise GhCommandError("REST-first PR metadata did not contain an exact URL and repository")
    if repo.casefold() != url_repo.casefold():
        raise GhCommandError(f"PR URL repository {url_repo} does not match helper repository {repo}")
    if repo_override and repo.casefold() != repo_override.casefold():
        raise GhCommandError(f"PR repository {repo} does not match --repo {repo_override}")

    try:
        number = int(data["number"])
    except (KeyError, TypeError, ValueError) as err:
        raise GhCommandError("REST-first PR metadata did not contain an exact PR number") from err
    if requested_number is not None:
        if number != requested_number:
            raise GhCommandError(
                f"REST-first PR helper returned PR {number}, expected {requested_number}"
            )

    head_sha = str(data.get("headRefOid") or "")
    if not head_sha:
        raise GhCommandError("REST-first PR metadata did not contain an exact head SHA")
    merged = data.get("merged") is True or bool(data.get("mergedAt"))
    raw_state = str(data.get("state") or "")
    state = "MERGED" if merged else raw_state.upper()
    closed = merged or state == "CLOSED"
    merge_state_status = str(data.get("mergeStateStatus") or "").upper()
    merge_state_available = merge_state_status in KNOWN_MERGE_STATES
    if not merge_state_available:
        merge_state_status = "UNKNOWN"
    review_decision = data.get("reviewDecision")

    return {
        "number": number,
        "url": pr_url,
        "repo": repo,
        "head_sha": head_sha,
        "head_branch": str(data.get("headRefName") or ""),
        "base_branch": str(data.get("baseRefName") or ""),
        "merge_commit_sha": str(data.get("mergeCommitOid") or "") if merged else "",
        "state": state,
        "merged": merged,
        "closed": closed,
        "draft": data.get("draft") is True,
        "mergeable": normalize_mergeable(data.get("mergeable")),
        "merge_state_status": merge_state_status,
        "review_decision": str(review_decision or ""),
        "metadata_availability": {
            "draft": isinstance(data.get("draft"), bool),
            "mergeable": isinstance(data.get("mergeable"), bool),
            "merge_state_status": merge_state_available,
            "review_decision": review_decision is not None,
        },
        "_read_diagnostic": payload["_watcher_diagnostic"],
    }


def extract_repo_from_pr_url(pr_url):
    parsed = urlparse(pr_url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 4 and parts[2] == "pull":
        return f"{parts[0]}/{parts[1]}"
    return None


def extract_pr_number_from_url(pr_url):
    parsed = urlparse(pr_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 4 and parts[2] == "pull" and parts[3].isdigit():
        return int(parts[3])
    return None


def load_state(path):
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as err:
            raise RuntimeError(f"State file is not valid JSON: {path}") from err
        if not isinstance(data, dict):
            raise RuntimeError(f"State file must contain an object: {path}")
        return data, False
    return {
        "pr": {},
        "started_at": None,
        "last_seen_head_sha": None,
        "retries_by_sha": {},
        "seen_issue_comment_ids": [],
        "seen_review_comment_ids": [],
        "seen_review_ids": [],
        "last_snapshot_at": None,
    }, True


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(payload)
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def default_state_file_for(pr):
    repo_slug = pr["repo"].replace("/", "-")
    return Path(f"/tmp/pr-babysit-{repo_slug}-pr{pr['number']}.json")


def get_pr_checks(pr_spec, repo):
    payload = pr_helper_json(
        "checks",
        pr_spec=pr_spec,
        repo=repo,
        allow_partial=True,
    )
    if not isinstance(payload.get("summary"), dict):
        raise GhCommandError("REST-first PR helper did not return a check summary")
    if not isinstance(payload.get("pr"), dict) or not payload.get("headSha"):
        raise GhCommandError("REST-first PR helper did not return an exact check head SHA")
    return payload


def nonnegative_int(value):
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def summarize_checks(checks, expected_head_sha):
    summary = checks.get("summary") if isinstance(checks, dict) else None
    if not isinstance(summary, dict):
        raise GhCommandError("REST-first PR check summary was not an object")
    pending_count = nonnegative_int(summary.get("pendingCount"))
    failed_count = nonnegative_int(summary.get("failingCount"))
    check_count = summary.get("checkRunCount")
    status_count = summary.get("statusCount")
    counts_valid = all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in (summary.get("pendingCount"), summary.get("failingCount"), check_count, status_count)
    )
    known_total = nonnegative_int(check_count) + nonnegative_int(status_count)
    passed_count = max(known_total - pending_count - failed_count, 0)
    unavailable = summary.get("unavailableComponents")
    if not isinstance(unavailable, list):
        unavailable = []
    counts_are_lower_bounds = summary.get("countsAreLowerBounds") is not False
    counts_complete = summary.get("countsComplete") is True
    head_sha = str(checks.get("headSha") or "")
    head_matches = bool(expected_head_sha) and head_sha == expected_head_sha
    evidence_complete = (
        counts_valid
        and counts_complete
        and not counts_are_lower_bounds
        and not unavailable
        and head_matches
    )
    return {
        "pending_count": pending_count,
        "failed_count": failed_count,
        "passed_count": passed_count,
        "all_terminal": evidence_complete and pending_count == 0,
        "evidence_complete": evidence_complete,
        "counts_are_lower_bounds": counts_are_lower_bounds,
        "unavailable_components": [str(item) for item in unavailable],
        "head_matches": head_matches,
    }


def get_workflow_runs_for_sha(repo, head_sha):
    endpoint = f"repos/{repo}/actions/runs"
    data = gh_json(
        ["api", endpoint, "-X", "GET", "-f", f"head_sha={head_sha}", "-f", "per_page=100"],
        repo=repo,
    )
    if not isinstance(data, dict):
        raise GhCommandError("Unexpected payload from actions runs API")
    runs = data.get("workflow_runs") or []
    if not isinstance(runs, list):
        raise GhCommandError("Expected `workflow_runs` to be a list")
    return runs


def failed_runs_from_workflow_runs(runs, head_sha):
    failed_runs = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        if str(run.get("head_sha") or "") != head_sha:
            continue
        conclusion = str(run.get("conclusion") or "")
        if conclusion not in FAILED_RUN_CONCLUSIONS:
            continue
        failed_runs.append(
            {
                "run_id": run.get("id"),
                "workflow_name": run.get("name") or run.get("display_title") or "",
                "status": str(run.get("status") or ""),
                "conclusion": conclusion,
                "html_url": str(run.get("html_url") or ""),
            }
        )
    failed_runs.sort(key=lambda item: (str(item.get("workflow_name") or ""), str(item.get("run_id") or "")))
    return failed_runs


def get_jobs_for_run(repo, run_id):
    endpoint = f"repos/{repo}/actions/runs/{run_id}/jobs"
    data = gh_json(["api", endpoint, "-X", "GET", "-f", "per_page=100"], repo=repo)
    if not isinstance(data, dict):
        raise GhCommandError("Unexpected payload from actions run jobs API")
    jobs = data.get("jobs") or []
    if not isinstance(jobs, list):
        raise GhCommandError("Expected `jobs` to be a list")
    return jobs


def failed_jobs_from_workflow_runs(repo, runs, head_sha):
    failed_jobs = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        if str(run.get("head_sha") or "") != head_sha:
            continue
        run_id = run.get("id")
        if run_id in (None, ""):
            continue
        run_status = str(run.get("status") or "")
        run_conclusion = str(run.get("conclusion") or "")
        if run_status.lower() == "completed" and run_conclusion not in FAILED_RUN_CONCLUSIONS:
            continue
        jobs = get_jobs_for_run(repo, run_id)
        for job in jobs:
            if not isinstance(job, dict):
                continue
            conclusion = str(job.get("conclusion") or "")
            if conclusion not in FAILED_RUN_CONCLUSIONS:
                continue
            job_id = job.get("id")
            logs_endpoint = None
            if job_id not in (None, ""):
                logs_endpoint = f"repos/{repo}/actions/jobs/{job_id}/logs"
            failed_jobs.append(
                {
                    "run_id": run_id,
                    "workflow_name": run.get("name") or run.get("display_title") or "",
                    "run_status": run_status,
                    "run_conclusion": run_conclusion,
                    "job_id": job_id,
                    "job_name": str(job.get("name") or ""),
                    "status": str(job.get("status") or ""),
                    "conclusion": conclusion,
                    "html_url": str(job.get("html_url") or ""),
                    "logs_endpoint": logs_endpoint,
                }
            )
    failed_jobs.sort(
        key=lambda item: (
            str(item.get("workflow_name") or ""),
            str(item.get("job_name") or ""),
            str(item.get("job_id") or ""),
        )
    )
    return failed_jobs


def get_authenticated_login():
    data = gh_json(["api", "user"])
    if not isinstance(data, dict) or not data.get("login"):
        raise GhCommandError("Unable to determine authenticated GitHub login from `gh api user`")
    return str(data["login"])


def comment_endpoints(repo, pr_number):
    return {
        "issue_comment": f"repos/{repo}/issues/{pr_number}/comments",
        "review_comment": f"repos/{repo}/pulls/{pr_number}/comments",
        "review": f"repos/{repo}/pulls/{pr_number}/reviews",
    }


def gh_api_list_paginated(endpoint, repo=None, per_page=100):
    items = []
    page = 1
    while True:
        sep = "&" if "?" in endpoint else "?"
        page_endpoint = f"{endpoint}{sep}per_page={per_page}&page={page}"
        payload = gh_json(["api", page_endpoint], repo=repo)
        if payload is None:
            break
        if not isinstance(payload, list):
            raise GhCommandError(f"Unexpected paginated payload from gh api {endpoint}")
        items.extend(payload)
        if len(payload) < per_page:
            break
        page += 1
    return items


def normalize_issue_comments(items):
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "kind": "issue_comment",
                "id": str(item.get("id") or ""),
                "author": extract_login(item.get("user")),
                "author_association": str(item.get("author_association") or ""),
                "created_at": str(item.get("created_at") or ""),
                "body": str(item.get("body") or ""),
                "path": None,
                "line": None,
                "url": str(item.get("html_url") or ""),
            }
        )
    return out


def normalize_review_comments(items):
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        line = item.get("line")
        if line is None:
            line = item.get("original_line")
        out.append(
            {
                "kind": "review_comment",
                "id": str(item.get("id") or ""),
                "author": extract_login(item.get("user")),
                "author_association": str(item.get("author_association") or ""),
                "created_at": str(item.get("created_at") or ""),
                "body": str(item.get("body") or ""),
                "path": item.get("path"),
                "line": line,
                "url": str(item.get("html_url") or ""),
            }
        )
    return out


def normalize_reviews(items):
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "kind": "review",
                "id": str(item.get("id") or ""),
                "author": extract_login(item.get("user")),
                "author_association": str(item.get("author_association") or ""),
                "created_at": str(item.get("submitted_at") or item.get("created_at") or ""),
                "body": str(item.get("body") or ""),
                "path": None,
                "line": None,
                "url": str(item.get("html_url") or ""),
            }
        )
    return out


def extract_login(user_obj):
    if isinstance(user_obj, dict):
        return str(user_obj.get("login") or "")
    return ""


def is_bot_login(login):
    normalized = str(login or "").casefold()
    return bool(normalized) and (
        normalized.endswith("[bot]") or normalized in configured_bot_logins()
    )


def is_actionable_review_bot_login(login):
    if not is_bot_login(login):
        return False
    lower_login = login.lower()
    return any(keyword in lower_login for keyword in REVIEW_BOT_LOGIN_KEYWORDS)


def is_external_human_review_author(item, authenticated_login):
    author = str(item.get("author") or "")
    if not author:
        return False
    if authenticated_login and author.casefold() == str(authenticated_login).casefold():
        return False
    return not is_bot_login(author)


def fetch_new_review_items(pr, state, fresh_state, authenticated_login=None):
    repo = pr["repo"]
    pr_number = pr["number"]
    endpoints = comment_endpoints(repo, pr_number)

    issue_payload = gh_api_list_paginated(endpoints["issue_comment"], repo=repo)
    review_comment_payload = gh_api_list_paginated(endpoints["review_comment"], repo=repo)
    review_payload = gh_api_list_paginated(endpoints["review"], repo=repo)

    issue_items = normalize_issue_comments(issue_payload)
    review_comment_items = normalize_review_comments(review_comment_payload)
    review_items = normalize_reviews(review_payload)
    all_items = issue_items + review_comment_items + review_items

    seen_issue = {str(x) for x in state.get("seen_issue_comment_ids") or []}
    seen_review_comment = {str(x) for x in state.get("seen_review_comment_ids") or []}
    seen_review = {str(x) for x in state.get("seen_review_ids") or []}

    # On a brand-new state file, surface existing review activity instead of
    # silently treating it as seen. This avoids missing already-pending review
    # feedback when monitoring starts after comments were posted.

    new_items = []
    for item in all_items:
        item_id = item.get("id")
        if not item_id:
            continue
        author = item.get("author") or ""
        if not author:
            continue
        if is_bot_login(author):
            if not is_actionable_review_bot_login(author):
                continue
        elif not is_external_human_review_author(item, authenticated_login):
            continue

        kind = item["kind"]
        if kind == "issue_comment" and item_id in seen_issue:
            continue
        if kind == "review_comment" and item_id in seen_review_comment:
            continue
        if kind == "review" and item_id in seen_review:
            continue

        new_items.append(item)
        if kind == "issue_comment":
            seen_issue.add(item_id)
        elif kind == "review_comment":
            seen_review_comment.add(item_id)
        elif kind == "review":
            seen_review.add(item_id)

    new_items.sort(key=lambda item: (item.get("created_at") or "", item.get("kind") or "", item.get("id") or ""))
    state["seen_issue_comment_ids"] = sorted(seen_issue)
    state["seen_review_comment_ids"] = sorted(seen_review_comment)
    state["seen_review_ids"] = sorted(seen_review)
    return new_items


def current_retry_count(state, head_sha):
    retries = state.get("retries_by_sha") or {}
    value = retries.get(head_sha, 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def set_retry_count(state, head_sha, count):
    retries = state.get("retries_by_sha")
    if not isinstance(retries, dict):
        retries = {}
    retries[head_sha] = int(count)
    state["retries_by_sha"] = retries


def unique_actions(actions):
    out = []
    seen = set()
    for action in actions:
        if action not in seen:
            out.append(action)
            seen.add(action)
    return out


def has_active_failed_job(failed_jobs):
    return any(str(job.get("run_status") or "").lower() != "completed" for job in failed_jobs)


def is_pr_ready_to_merge(pr, checks_summary, new_review_items):
    if pr["closed"] or pr["merged"]:
        return False
    if pr.get("draft"):
        return False
    if not checks_summary["all_terminal"]:
        return False
    if checks_summary["failed_count"] > 0 or checks_summary["pending_count"] > 0:
        return False
    if new_review_items:
        return False
    availability = pr.get("metadata_availability")
    if not isinstance(availability, dict) or not all(
        availability.get(field) is True
        for field in ("draft", "mergeable", "merge_state_status", "review_decision")
    ):
        return False
    if str(pr.get("mergeable") or "") != "MERGEABLE":
        return False
    if str(pr.get("merge_state_status") or "") in MERGE_CONFLICT_OR_BLOCKING_STATES:
        return False
    return str(pr.get("review_decision") or "") not in MERGE_BLOCKING_REVIEW_DECISIONS


def is_review_readiness_unavailable(pr, checks_summary, new_review_items):
    availability = pr.get("metadata_availability")
    return (
        not pr["closed"]
        and not pr["merged"]
        and not pr.get("draft")
        and checks_summary["all_terminal"]
        and checks_summary["failed_count"] == 0
        and checks_summary["pending_count"] == 0
        and not new_review_items
        and isinstance(availability, dict)
        and all(
            availability.get(field) is True
            for field in ("draft", "mergeable", "merge_state_status")
        )
        and str(pr.get("mergeable") or "") == "MERGEABLE"
        and str(pr.get("merge_state_status") or "")
        not in MERGE_CONFLICT_OR_BLOCKING_STATES
        and availability.get("review_decision") is not True
    )


def recommend_actions(pr, checks_summary, failed_runs, failed_jobs, new_review_items, retries_used, max_retries):
    actions = []
    if pr["closed"] or pr["merged"]:
        if new_review_items:
            actions.append("process_review_comment")
        actions.append("stop_pr_closed")
        return unique_actions(actions)

    if is_pr_ready_to_merge(pr, checks_summary, new_review_items):
        actions.append("ready_to_merge")
        return unique_actions(actions)

    has_failed_pr_checks = (
        checks_summary["failed_count"] > 0 and checks_summary.get("head_matches") is True
    ) or has_active_failed_job(failed_jobs)

    if (
        not has_failed_pr_checks
        and is_review_readiness_unavailable(pr, checks_summary, new_review_items)
    ):
        actions.append("review_readiness_unavailable")

    if new_review_items:
        actions.append("process_review_comment")

    if checks_summary.get("evidence_complete") is not True:
        actions.append("check_evidence_incomplete")

    if has_failed_pr_checks:
        if checks_summary["all_terminal"] and retries_used >= max_retries:
            actions.append("stop_exhausted_retries")
        else:
            actions.append("diagnose_ci_failure")
            if checks_summary["all_terminal"] and failed_runs and retries_used < max_retries:
                actions.append("retry_failed_checks")

    if not actions:
        actions.append("idle")
    return unique_actions(actions)


def collect_snapshot(args):
    pr = resolve_pr(args.pr, repo_override=args.repo)
    pr_diagnostic = pr.pop("_read_diagnostic", None)
    state_path = Path(args.state_file) if args.state_file else default_state_file_for(pr)
    state, fresh_state = load_state(state_path)

    if not state.get("started_at"):
        state["started_at"] = int(time.time())

    authenticated_login = get_authenticated_login()
    new_review_items = fetch_new_review_items(
        pr,
        state,
        fresh_state=fresh_state,
        authenticated_login=authenticated_login,
    )
    # Surface review feedback before drilling into CI and mergeability details.
    # That keeps the babysitter responsive to new comments even when other
    # actions are also available.
    # After resolving `--pr auto`, give the REST-first checks helper the
    # concrete PR number so both reads stay pinned to one target.
    checks = get_pr_checks(str(pr["number"]), repo=pr["repo"])
    checks_diagnostic = checks.pop("_watcher_diagnostic", None)
    checks_summary = summarize_checks(checks, expected_head_sha=pr["head_sha"])
    workflow_runs = get_workflow_runs_for_sha(pr["repo"], pr["head_sha"])
    failed_runs = failed_runs_from_workflow_runs(workflow_runs, pr["head_sha"])
    failed_jobs = failed_jobs_from_workflow_runs(pr["repo"], workflow_runs, pr["head_sha"])

    retries_used = current_retry_count(state, pr["head_sha"])
    actions = recommend_actions(
        pr,
        checks_summary,
        failed_runs,
        failed_jobs,
        new_review_items,
        retries_used,
        args.max_flaky_retries,
    )

    state["pr"] = {"repo": pr["repo"], "number": pr["number"]}
    state["last_seen_head_sha"] = pr["head_sha"]
    state["last_snapshot_at"] = int(time.time())
    save_state(state_path, state)

    snapshot = {
        "pr": pr,
        "checks": checks_summary,
        "failed_runs": failed_runs,
        "failed_jobs": failed_jobs,
        "new_review_items": new_review_items,
        "actions": actions,
        "retry_state": {
            "current_sha_retries_used": retries_used,
            "max_flaky_retries": args.max_flaky_retries,
        },
        "read_diagnostics": {
            "pr": pr_diagnostic,
            "checks": checks_diagnostic,
        },
    }
    return snapshot, state_path


def retry_failed_now(args):
    snapshot, state_path = collect_snapshot(args)
    pr = snapshot["pr"]
    checks_summary = snapshot["checks"]
    failed_runs = snapshot["failed_runs"]
    retries_used = snapshot["retry_state"]["current_sha_retries_used"]
    max_retries = snapshot["retry_state"]["max_flaky_retries"]

    result = {
        "snapshot": snapshot,
        "state_file": str(state_path),
        "rerun_attempted": False,
        "rerun_count": 0,
        "rerun_run_ids": [],
        "reason": None,
    }

    if pr["closed"] or pr["merged"]:
        result["reason"] = "pr_closed"
        return result
    if checks_summary.get("evidence_complete") is not True:
        result["reason"] = "check_evidence_incomplete"
        return result
    if checks_summary["failed_count"] <= 0:
        result["reason"] = "no_failed_pr_checks"
        return result
    if not failed_runs:
        result["reason"] = "no_failed_runs"
        return result
    if not checks_summary["all_terminal"]:
        result["reason"] = "checks_still_pending"
        return result
    if retries_used >= max_retries:
        result["reason"] = "retry_budget_exhausted"
        return result

    for run in failed_runs:
        run_id = run.get("run_id")
        if run_id in (None, ""):
            continue
        gh_text(["run", "rerun", str(run_id), "--failed"], repo=pr["repo"])
        result["rerun_run_ids"].append(run_id)

    if result["rerun_run_ids"]:
        state, _ = load_state(state_path)
        new_count = current_retry_count(state, pr["head_sha"]) + 1
        set_retry_count(state, pr["head_sha"], new_count)
        state["last_snapshot_at"] = int(time.time())
        save_state(state_path, state)
        result["rerun_attempted"] = True
        result["rerun_count"] = len(result["rerun_run_ids"])
        result["reason"] = "rerun_triggered"
    else:
        result["reason"] = "failed_runs_missing_ids"

    return result


def print_json(obj):
    sys.stdout.write(json.dumps(obj, sort_keys=True) + "\n")
    sys.stdout.flush()


def print_event(event, payload):
    print_json({"event": event, "payload": payload})


def is_ci_green(snapshot):
    checks = snapshot.get("checks") or {}
    return (
        bool(checks.get("all_terminal"))
        and int(checks.get("failed_count") or 0) == 0
        and int(checks.get("pending_count") or 0) == 0
    )


def snapshot_change_key(snapshot):
    pr = snapshot.get("pr") or {}
    checks = snapshot.get("checks") or {}
    review_items = snapshot.get("new_review_items") or []
    return (
        str(pr.get("head_sha") or ""),
        str(pr.get("state") or ""),
        str(pr.get("mergeable") or ""),
        str(pr.get("merge_state_status") or ""),
        str(pr.get("review_decision") or ""),
        int(checks.get("passed_count") or 0),
        int(checks.get("failed_count") or 0),
        int(checks.get("pending_count") or 0),
        tuple(
            (str(item.get("kind") or ""), str(item.get("id") or ""))
            for item in review_items
            if isinstance(item, dict)
        ),
        tuple(snapshot.get("actions") or []),
    )


def run_watch(args):
    poll_seconds = args.poll_seconds
    last_change_key = None
    while True:
        snapshot, state_path = collect_snapshot(args)
        print_event(
            "snapshot",
            {
                "snapshot": snapshot,
                "state_file": str(state_path),
                "next_poll_seconds": poll_seconds,
            },
        )
        actions = set(snapshot.get("actions") or [])
        if (
            "stop_pr_closed" in actions
            or "stop_exhausted_retries" in actions
        ):
            print_event("stop", {"actions": snapshot.get("actions"), "pr": snapshot.get("pr")})
            return 0

        current_change_key = snapshot_change_key(snapshot)
        changed = current_change_key != last_change_key
        green = is_ci_green(snapshot)
        pr = snapshot.get("pr") or {}
        pr_open = not bool(pr.get("closed")) and not bool(pr.get("merged"))

        if not green or pr_open:
            poll_seconds = args.poll_seconds
        elif changed or last_change_key is None:
            poll_seconds = args.poll_seconds

        last_change_key = current_change_key
        time.sleep(poll_seconds)


def main():
    args = parse_args()
    try:
        if args.retry_failed_now:
            print_json(retry_failed_now(args))
            return 0
        if args.watch:
            return run_watch(args)
        snapshot, state_path = collect_snapshot(args)
        snapshot["state_file"] = str(state_path)
        print_json(snapshot)
        return 0
    except (GhCommandError, RuntimeError, ValueError) as err:
        sys.stderr.write(f"gh_pr_watch.py error: {err}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("gh_pr_watch.py interrupted\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
