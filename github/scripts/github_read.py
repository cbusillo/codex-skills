#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Shared REST-first readers for GitHub metadata used by public helpers."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.parse
from collections.abc import Callable
from typing import Any, Optional

import github_api as github_api_core


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
DEFAULT_GH = os.environ.get("GITHUB_READ_GH") or str(SCRIPT_DIR / "gh-with-env-token")
EXPECTED_ACTOR = os.environ.get("GH_WITH_ENV_TOKEN_EXPECTED_LOGIN") or "shiny-code-bot"
MERGE_STATE_STATUS = {
    "behind": "BEHIND",
    "blocked": "BLOCKED",
    "clean": "CLEAN",
    "dirty": "DIRTY",
    "draft": "DRAFT",
    "has_hooks": "HAS_HOOKS",
    "unknown": "UNKNOWN",
    "unstable": "UNSTABLE",
}


class GitHubReadError(Exception):
    def __init__(
        self,
        message: str,
        *,
        result: github_api_core.ApiResult,
        diagnostics: dict[str, Any],
    ) -> None:
        super().__init__(github_api_core.redact_string(message))
        self.result = result
        self.diagnostics = diagnostics


class GitHubReadShapeError(Exception):
    pass


class GitHubReader:
    def __init__(
        self,
        *,
        gh_cmd: str = DEFAULT_GH,
        expected_actor: Optional[str] = EXPECTED_ACTOR,
        operation: str = "github.read",
        actor: Optional[str] = None,
        gh_prefix_args: Optional[list[str]] = None,
        strict_actor: bool = False,
    ) -> None:
        self.gh_cmd = gh_cmd
        self.expected_actor = expected_actor
        self.operation = operation
        self.actor = actor
        self.request_actor = actor
        self.gh_prefix_args = list(gh_prefix_args or [])
        self.strict_actor = strict_actor
        self.completed_steps: list[str] = []
        self.requests: list[dict[str, Any]] = []
        self.results: list[github_api_core.ApiResult] = []
        self.failed_results: list[github_api_core.ApiResult] = []
        self.degraded_reasons: list[dict[str, str]] = []
        self.last_result: Optional[github_api_core.ApiResult] = None

    def request(self, method: str, path: str, *, step: str) -> github_api_core.ApiResult:
        result = github_api_core.call_gh_with_retry(
            method,
            path,
            gh_cmd=self.gh_cmd,
            gh_prefix_args=self.gh_prefix_args,
            operation=self.operation,
            actor=self.request_actor,
            expected_actor=self.expected_actor,
            bucket="rest_core",
            completed_steps=list(self.completed_steps),
            failed_step=step,
            is_write=False,
        )
        actor_mismatch = False
        if result.actor:
            if self.expected_actor and self.expected_actor.casefold() != result.actor.casefold():
                self.mark_degraded(
                    "actor",
                    "actor_mismatch",
                    f"GitHub read ran as '{result.actor}', expected '{self.expected_actor}'",
                )
                actor_mismatch = True
            elif self.actor and self.actor.casefold() != result.actor.casefold():
                self.mark_degraded(
                    "actor",
                    "actor_changed",
                    f"GitHub actor changed from '{self.actor}' to '{result.actor}' during one read operation",
                )
            self.actor = result.actor
        if actor_mismatch and self.strict_actor:
            result.ok = False
            result.expected_actor = self.expected_actor
            result.failed_step = step
            result.failure = github_api_core.FailureDetail(
                cause="actor_mismatch",
                message=(
                    f"Authenticated actor '{result.actor}' does not match "
                    f"expected actor '{self.expected_actor}'"
                ),
                retryable=False,
                fallback_eligible=False,
                disposition="stop",
                completed_steps=list(self.completed_steps),
                failed_step=step,
                request_id=result.request_id,
            )
        self.last_result = result
        self.results.append(result)
        self.requests.append(request_diagnostic(result, method=method, path=path, step=step))
        if not result.ok:
            self.failed_results.append(result)
            message = result.failure.message if result.failure else "GitHub REST read failed"
            self.mark_degraded(step, result.failure.cause if result.failure else "read_failed", message)
            raise GitHubReadError(message, result=result, diagnostics=self.diagnostics())
        self.completed_steps.append(step)
        return result

    def get_json(self, path: str, *, step: str) -> Any:
        return self.request("GET", path, step=step).body

    def get_text(self, path: str, *, step: str) -> str:
        body = self.request("GET", path, step=step).body
        if not isinstance(body, str):
            self.invalid_response(step, f"GitHub {step} response was not text")
        return body

    def paged_json(
        self,
        path: str,
        *,
        step_prefix: str,
        params: Optional[dict[str, Any]] = None,
        collection_key: Optional[str] = None,
        limit: Optional[int] = None,
        item_filter: Optional[Callable[[dict[str, Any]], bool]] = None,
        required_query_params: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        if limit is not None and limit <= 0:
            return []
        per_page = min(100, max(limit or 100, 1))
        base_params = {**(params or {}), "per_page": per_page}
        page = 1
        next_path: Optional[str] = path_with_query(path, {**base_params, "page": page})
        items: list[dict[str, Any]] = []
        seen_paths: set[str] = set()

        while next_path:
            if next_path in seen_paths:
                raise GitHubReadShapeError(f"GitHub {step_prefix} pagination repeated the same page")
            seen_paths.add(next_path)
            result = self.request("GET", next_path, step=f"{step_prefix}_page_{page}")
            try:
                page_items = collection_items(result.body, collection_key=collection_key, step=step_prefix)
            except GitHubReadShapeError as exc:
                self.invalid_response(step_prefix, str(exc))
            for item in page_items:
                if item_filter is not None and not item_filter(item):
                    continue
                items.append(item)
                if limit is not None and len(items) >= limit:
                    return items[:limit]

            link_header = result.headers.get("link")
            if link_header is not None:
                next_path = next_link(link_header)
                if next_path and required_query_params:
                    next_path = replace_query_params(next_path, required_query_params)
            elif len(page_items) < per_page:
                next_path = None
            else:
                page += 1
                next_path = path_with_query(path, {**base_params, "page": page})
                continue
            page += 1

        return items if limit is None else items[:limit]

    def invalid_response(self, component: str, message: str) -> None:
        self.mark_degraded(component, "invalid_response", message)
        raise GitHubReadShapeError(message)

    def mark_degraded(self, component: str, code: str, message: str) -> None:
        reason = {
            "component": component,
            "code": code,
            "message": github_api_core.redact_string(message),
        }
        if reason not in self.degraded_reasons:
            self.degraded_reasons.append(reason)

    def diagnostics(self) -> dict[str, Any]:
        quota = None
        for request in reversed(self.requests):
            if request.get("quota"):
                quota = request["quota"]
                break
        failed_components = sorted({
            str(reason["component"])
            for reason in self.degraded_reasons
            if reason.get("component")
        })
        retry_summary = self.retry_summary()
        return {
            "transport": "rest_api",
            "bucket": "rest_core",
            "actor": self.actor,
            "expectedActor": self.expected_actor,
            "requestCount": len(self.requests),
            "requests": self.requests,
            "quota": quota,
            "degraded": bool(self.degraded_reasons),
            "degradedComponents": failed_components,
            "degradedReasons": self.degraded_reasons,
            "retry": retry_summary.as_dict() if retry_summary is not None else None,
        }

    def retry_summary(self) -> Optional[github_api_core.RetrySummary]:
        summaries = [
            result.retry_summary
            for result in self.results
            if result.retry_summary is not None
        ]
        return github_api_core.aggregate_retry_summaries(
            summaries,
            failed=bool(self.failed_results),
        )


def automation_only_gh_prefix_args() -> list[str]:
    return ["--require-automation-auth"]


def request_diagnostic(
    result: github_api_core.ApiResult,
    *,
    method: str,
    path: str,
    step: str,
) -> dict[str, Any]:
    payload = result.as_dict()
    diagnostic: dict[str, Any] = {
        "step": step,
        "method": method.upper(),
        "endpoint": github_api_core.redact_path(path),
        "ok": result.ok,
        "status": result.status,
        "requestId": payload.get("request_id"),
        "bucket": payload.get("bucket"),
        "actor": payload.get("actor"),
        "expectedActor": payload.get("expected_actor"),
        "quota": payload.get("quota"),
        "retryable": payload.get("retryable", False),
        "retryAt": payload.get("retry_at"),
        "retryAfter": payload.get("retry_after"),
        "attempts": payload.get("attempts"),
        "elapsedWait": payload.get("elapsed_wait"),
        "retryEligible": payload.get("retry_eligible"),
        "outcomeCertainty": payload.get("outcome_certainty"),
        "recommendedNextAction": payload.get("recommended_next_action"),
        "lastActor": payload.get("last_actor"),
        "lastBucket": payload.get("last_bucket"),
        "reconciliation": payload.get("reconciliation"),
        "effectiveDeadline": payload.get("effective_deadline"),
        "retryExhaustedReason": payload.get("retry_exhausted_reason"),
    }
    if result.failure:
        diagnostic["cause"] = result.failure.cause
        diagnostic["message"] = github_api_core.redact_string(result.failure.message)
    return {key: value for key, value in diagnostic.items() if value is not None}


def path_with_query(path: str, params: dict[str, Any]) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{urllib.parse.urlencode(params, doseq=True)}"


def replace_query_params(path: str, params: dict[str, Any]) -> str:
    parsed = urllib.parse.urlsplit(path)
    replacements = {str(key): str(value) for key, value in params.items()}
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key not in replacements
    ]
    query.extend(replacements.items())
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


def next_link(link_header: Optional[str]) -> Optional[str]:
    if not link_header:
        return None
    for item in link_header.split(","):
        if 'rel="next"' not in item:
            continue
        match = re.search(r"<([^>]+)>", item)
        if match:
            return match.group(1)
    return None


def collection_items(body: Any, *, collection_key: Optional[str], step: str) -> list[dict[str, Any]]:
    value = body.get(collection_key) if collection_key and isinstance(body, dict) else body
    if not isinstance(value, list):
        raise GitHubReadShapeError(f"GitHub {step} response did not contain a list")
    if not all(isinstance(item, dict) for item in value):
        raise GitHubReadShapeError(f"GitHub {step} response contained a non-object item")
    return value


def resolve_repo(repo_root: pathlib.Path, explicit_repo: Optional[str] = None) -> str:
    if explicit_repo:
        if re.fullmatch(r"[^/\s]+/[^/\s]+", explicit_repo):
            return explicit_repo.removesuffix(".git")
        raise ValueError("repository must use OWNER/REPO form")
    return resolve_repo_from_remote(repo_root, "origin")


def resolve_repo_from_remote(repo_root: pathlib.Path, remote_name: str) -> str:
    process = subprocess.run(
        ["git", "remote", "get-url", remote_name],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise ValueError(f"could not resolve GitHub repository from {remote_name}; pass --repo OWNER/REPO")
    remote = process.stdout.strip()
    return repo_from_remote_url(remote)


def repo_from_remote_url(remote: str) -> str:
    expected_host = os.environ.get("GH_HOST") or "github.com"
    if "://" in remote:
        parsed = urllib.parse.urlparse(remote)
        host = parsed.hostname or ""
        remote_path = parsed.path
    else:
        match = re.fullmatch(r"(?:[^@]+@)?([^:]+):(.+)", remote)
        if not match:
            raise ValueError("could not resolve GitHub repository from remote URL; pass --repo OWNER/REPO")
        host = match.group(1)
        remote_path = match.group(2)
    if host.casefold() != expected_host.casefold():
        raise ValueError("could not resolve GitHub repository from remote URL; pass --repo OWNER/REPO")
    parts = [part for part in remote_path.strip("/").removesuffix(".git").split("/") if part]
    if len(parts) != 2:
        raise ValueError("could not resolve GitHub repository from remote URL; pass --repo OWNER/REPO")
    return f"{parts[0]}/{parts[1]}"


def normalize_pull_request(item: dict[str, Any]) -> dict[str, Any]:
    head = item.get("head") or {}
    base = item.get("base") or {}
    mergeable_state = item.get("mergeable_state")
    merged = item.get("merged")
    if merged is None and item.get("merged_at"):
        merged = True
    return {
        "number": item.get("number"),
        "title": item.get("title"),
        "state": item.get("state"),
        "draft": item.get("draft"),
        "isDraft": item.get("draft"),
        "merged": merged,
        "mergedAt": item.get("merged_at"),
        "mergeCommitOid": item.get("merge_commit_sha"),
        "mergeable": item.get("mergeable"),
        "mergeable_state": mergeable_state,
        "mergeStateStatus": MERGE_STATE_STATUS.get(str(mergeable_state), str(mergeable_state).upper()) if mergeable_state else None,
        "reviewDecision": item.get("reviewDecision"),
        "statusCheckRollup": item.get("statusCheckRollup"),
        "labels": item.get("labels") or [],
        "url": item.get("html_url"),
        "baseRefName": base.get("ref"),
        "headRefName": head.get("ref"),
        "headRefOid": head.get("sha"),
        "headRepository": (head.get("repo") or {}).get("full_name"),
        "baseRepository": (base.get("repo") or {}).get("full_name"),
    }


def actions_ids(url: Any) -> tuple[Optional[int], Optional[int]]:
    value = str(url or "")
    run_match = re.search(r"/actions/runs/(\d+)", value)
    job_match = re.search(r"/job/(\d+)", value)
    return (
        int(run_match.group(1)) if run_match else None,
        int(job_match.group(1)) if job_match else None,
    )


def normalize_check_run(item: dict[str, Any]) -> dict[str, Any]:
    details_url = item.get("details_url") or item.get("html_url")
    run_id, job_id = actions_ids(details_url)
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "status": item.get("status"),
        "conclusion": item.get("conclusion"),
        "detailsUrl": details_url,
        "startedAt": item.get("started_at"),
        "completedAt": item.get("completed_at"),
        "workflowName": (item.get("app") or {}).get("name"),
        "checkSuiteId": (item.get("check_suite") or {}).get("id"),
        "runId": run_id,
        "jobId": job_id,
    }


def normalize_status(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "context": item.get("context"),
        "state": item.get("state"),
        "description": item.get("description"),
        "targetUrl": item.get("target_url"),
        "createdAt": item.get("created_at"),
        "updatedAt": item.get("updated_at"),
    }


def latest_status_events(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, tuple[tuple[str, int], dict[str, Any]]] = {}
    for item in items:
        context = str(item.get("context") or "")
        key = context.casefold()
        timestamp = str(item.get("updated_at") or item.get("created_at") or "")
        try:
            identifier = int(item.get("id") or 0)
        except (TypeError, ValueError):
            identifier = 0
        freshness = (timestamp, identifier)
        previous = latest.get(key)
        if previous is None or freshness > previous[0]:
            latest[key] = (freshness, item)
    return [entry[1] for entry in sorted(latest.values(), key=lambda entry: entry[0], reverse=True)]


def normalize_issue(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": item.get("number"),
        "title": item.get("title"),
        "state": str(item.get("state") or "").upper(),
        "labels": item.get("labels") or [],
        "url": item.get("html_url"),
        "updatedAt": item.get("updated_at"),
    }


def normalize_workflow_run(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "databaseId": item.get("id"),
        "name": item.get("name"),
        "workflowName": item.get("name"),
        "displayTitle": item.get("display_title"),
        "status": item.get("status"),
        "conclusion": item.get("conclusion"),
        "headBranch": item.get("head_branch"),
        "headSha": item.get("head_sha"),
        "event": item.get("event"),
        "createdAt": item.get("created_at"),
        "updatedAt": item.get("updated_at"),
        "url": item.get("html_url"),
        "runAttempt": item.get("run_attempt"),
    }


def normalize_workflow_job(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "runId": item.get("run_id"),
        "runAttempt": item.get("run_attempt"),
        "name": item.get("name"),
        "status": item.get("status"),
        "conclusion": item.get("conclusion"),
        "startedAt": item.get("started_at"),
        "completedAt": item.get("completed_at"),
        "url": item.get("html_url"),
    }


def normalize_repository(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "nameWithOwner": item.get("full_name"),
        "defaultBranchRef": {"name": item.get("default_branch")},
        "deleteBranchOnMerge": item.get("delete_branch_on_merge"),
        "url": item.get("html_url"),
    }


def list_pull_requests(
    reader: GitHubReader,
    repo: str,
    *,
    state: str = "open",
    limit: int = 20,
    head_branch: Optional[str] = None,
    head_owner: Optional[str] = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"state": state}
    if head_branch:
        params["head"] = f"{head_owner or repo.split('/', 1)[0]}:{head_branch}"
    items = reader.paged_json(
        f"/repos/{repo}/pulls",
        step_prefix="pull_requests",
        params=params,
        limit=limit,
    )
    return [normalize_pull_request(item) for item in items]


def pull_request_checks(reader: GitHubReader, repo: str, number: int) -> dict[str, Any]:
    pull = reader.get_json(f"/repos/{repo}/pulls/{number}", step="pull_request")
    if not isinstance(pull, dict) or not isinstance(pull.get("head"), dict) or not pull["head"].get("sha"):
        reader.invalid_response("pull_request", "GitHub pull request response did not contain a head SHA")
    sha = str(pull["head"]["sha"])
    availability = {"checkRuns": True, "commitStatuses": True, "combinedStatus": True}
    try:
        check_runs = reader.paged_json(
            f"/repos/{repo}/commits/{sha}/check-runs",
            step_prefix="check_runs",
            collection_key="check_runs",
        )
    except (GitHubReadError, GitHubReadShapeError):
        availability["checkRuns"] = False
        check_runs = []
    try:
        statuses = reader.paged_json(
            f"/repos/{repo}/commits/{sha}/statuses",
            step_prefix="commit_statuses",
        )
    except (GitHubReadError, GitHubReadShapeError):
        availability["commitStatuses"] = False
        statuses = []
    try:
        combined = reader.get_json(f"/repos/{repo}/commits/{sha}/status", step="combined_status")
        if not isinstance(combined, dict):
            reader.invalid_response("combined_status", "GitHub combined status response was not an object")
    except (GitHubReadError, GitHubReadShapeError):
        availability["combinedStatus"] = False
        combined = {}
    statuses = latest_status_events(statuses)
    normalized_checks = [normalize_check_run(item) for item in check_runs]
    normalized_statuses = [normalize_status(item) for item in statuses]
    failure_conclusions = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
    failing = [item for item in normalized_checks if item.get("conclusion") in failure_conclusions]
    pending = [item for item in normalized_checks if item.get("status") != "completed"]
    failed_statuses = [item for item in normalized_statuses if item.get("state") in {"failure", "error"}]
    pending_statuses = [item for item in normalized_statuses if item.get("state") == "pending"]
    combined_state = combined.get("state")
    counts_complete = availability["checkRuns"] and availability["commitStatuses"]
    unavailable_components = [name for name, available in availability.items() if not available]
    return {
        "repo": repo,
        "pr": normalize_pull_request(pull),
        "headSha": sha,
        "summary": {
            "checkRunCount": len(normalized_checks) if availability["checkRuns"] else None,
            "statusCount": len(normalized_statuses) if availability["commitStatuses"] else None,
            "failingCount": len(failing) + len(failed_statuses),
            "pendingCount": len(pending) + len(pending_statuses),
            "countsComplete": counts_complete,
            "countsAreLowerBounds": not counts_complete,
            "combinedState": combined_state if normalized_statuses and availability["combinedStatus"] else None,
            "combinedStateRaw": combined_state,
            "legacyStatusesPresent": bool(normalized_statuses),
            "availability": availability,
            "unavailableComponents": unavailable_components,
        },
        "checkRuns": normalized_checks,
        "statuses": normalized_statuses,
    }


def list_issues(reader: GitHubReader, repo: str, *, state: str = "open", limit: int = 30) -> list[dict[str, Any]]:
    items = reader.paged_json(
        f"/repos/{repo}/issues",
        step_prefix="issues",
        params={"state": state},
        limit=limit,
        item_filter=lambda item: item.get("pull_request") is None,
    )
    return [normalize_issue(item) for item in items]


def list_workflow_runs(
    reader: GitHubReader,
    repo: str,
    *,
    branch: Optional[str] = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {}
    if branch:
        params["branch"] = branch
    items = reader.paged_json(
        f"/repos/{repo}/actions/runs",
        step_prefix="workflow_runs",
        params=params,
        collection_key="workflow_runs",
        limit=limit,
    )
    return [normalize_workflow_run(item) for item in items]


def workflow_run(reader: GitHubReader, repo: str, run_id: int) -> dict[str, Any]:
    item = reader.get_json(f"/repos/{repo}/actions/runs/{run_id}", step=f"workflow_run_{run_id}")
    if not isinstance(item, dict):
        reader.invalid_response("workflowRun", "GitHub workflow run response was not an object")
    return normalize_workflow_run(item)


def workflow_jobs(reader: GitHubReader, repo: str, run_id: int) -> list[dict[str, Any]]:
    items = reader.paged_json(
        f"/repos/{repo}/actions/runs/{run_id}/jobs",
        step_prefix=f"workflow_run_{run_id}_jobs",
        params={"filter": "latest"},
        collection_key="jobs",
    )
    return [normalize_workflow_job(item) for item in items]


def job_log(reader: GitHubReader, repo: str, job_id: int) -> str:
    text = reader.get_text(f"/repos/{repo}/actions/jobs/{job_id}/logs", step=f"job_{job_id}_log")
    if text.startswith("PK"):
        reader.invalid_response("workflowLog", "GitHub job log response was an unsupported zip archive")
    return text


def repository(reader: GitHubReader, repo: str) -> dict[str, Any]:
    item = reader.get_json(f"/repos/{repo}", step="repository")
    if not isinstance(item, dict):
        reader.invalid_response("repository", "GitHub repository response was not an object")
    return normalize_repository(item)


def redacted_repository_context(item: dict[str, Any]) -> dict[str, Any]:
    private = item.get("private") if isinstance(item.get("private"), bool) else None
    visibility = item.get("visibility") if isinstance(item.get("visibility"), str) else None
    if visibility is None:
        visibility = "private" if private is True else "public" if private is False else "unknown"
    return {
        "visibility": visibility,
        "isPrivate": private,
    }


def repository_scanning_disabled(item: dict[str, Any]) -> bool:
    security = item.get("security_and_analysis")
    scanning_feature = security.get("secret_scanning") if isinstance(security, dict) else None
    feature_status = scanning_feature.get("status") if isinstance(scanning_feature, dict) else None
    return isinstance(feature_status, str) and feature_status.casefold() == "disabled"


def redacted_security_signal(
    repository_context: dict[str, Any],
    *,
    status: str,
    reason: Optional[str],
    open_alert_count: Optional[int] = None,
    count_is_lower_bound: bool = False,
) -> dict[str, Any]:
    return {
        "signal": "redacted_security_status",
        "status": status,
        "reason": reason,
        "openAlertCount": open_alert_count,
        "openAlertCountIsLowerBound": count_is_lower_bound,
        "literalValuesHidden": True,
        "repository": repository_context,
    }


def verify_security_status_actor(reader: GitHubReader) -> bool:
    identity = reader.get_json("/user", step="security_status_actor")
    if not isinstance(identity, dict) or not isinstance(identity.get("login"), str):
        reader.invalid_response("security_status_actor", "GitHub user response did not contain a login")
    login = identity["login"]
    reader.actor = login
    reader.request_actor = login
    if reader.last_result is not None:
        reader.last_result.actor = login
        if reader.last_result.retry_summary is not None:
            reader.last_result.retry_summary.last_actor = login
    if reader.requests:
        reader.requests[-1]["actor"] = login
        reader.requests[-1]["lastActor"] = login
    if reader.expected_actor and reader.expected_actor.casefold() != login.casefold():
        reader.mark_degraded(
            "actor",
            "actor_mismatch",
            f"GitHub secret-scanning read ran as '{login}', expected '{reader.expected_actor}'",
        )
        return False
    return True


def redacted_secret_scanning_status(reader: GitHubReader, repo: str, *, limit: int = 100) -> dict[str, Any]:
    if limit <= 0 or limit > 1000:
        raise ValueError("secret-scanning alert limit must be between 1 and 1000")

    unknown_repository = {
        "visibility": "unknown",
        "isPrivate": None,
    }
    if not verify_security_status_actor(reader):
        return redacted_security_signal(
            unknown_repository,
            status="unavailable",
            reason="actor_mismatch",
        )

    try:
        repository_item = reader.get_json(f"/repos/{repo}", step="security_status_repository")
    except GitHubReadError as exc:
        cause = exc.result.failure.cause if exc.result.failure else None
        if cause in {"permission_denied", "not_found"} or exc.result.status in {403, 404}:
            return redacted_security_signal(
                unknown_repository,
                status="unavailable",
                reason="repository_permission_or_visibility_limited",
            )
        raise
    if not isinstance(repository_item, dict):
        reader.invalid_response(
            "security_status_repository",
            "GitHub repository response was not an object",
        )
    repository_context = redacted_repository_context(repository_item)
    if repository_context["visibility"] == "public":
        return redacted_security_signal(
            repository_context,
            status="unavailable",
            reason="public_repository_alert_api_unavailable",
        )
    if repository_scanning_disabled(repository_item):
        return redacted_security_signal(
            repository_context,
            status="not_enabled",
            reason="scanning_disabled",
        )

    try:
        alerts = reader.paged_json(
            f"/repos/{repo}/secret-scanning/alerts",
            step_prefix="security_alerts",
            params={"state": "open", "hide_secret": "true"},
            limit=limit,
            required_query_params={"state": "open", "hide_secret": "true"},
        )
    except GitHubReadError as exc:
        cause = exc.result.failure.cause if exc.result.failure else None
        if cause == "permission_denied":
            return redacted_security_signal(
                repository_context,
                status="unavailable",
                reason="permission_limited",
            )
        if cause == "not_found" or exc.result.status == 404:
            return redacted_security_signal(
                repository_context,
                status="unavailable",
                reason="alert_endpoint_404_ambiguous",
            )
        raise

    open_alert_count = len(alerts)
    return redacted_security_signal(
        repository_context,
        status="findings" if open_alert_count else "clean",
        reason=None,
        open_alert_count=open_alert_count,
        count_is_lower_bound=open_alert_count >= limit,
    )


def parse_args() -> argparse.Namespace:
    parser = github_api_core.TerminalArgumentParser(description="Shared paged REST readers for GitHub metadata.")
    parser.add_argument("--gh", default=DEFAULT_GH, help="Path to gh or gh-with-env-token.")
    parser.add_argument("--repo", help="Repository in OWNER/REPO form. Defaults to the origin remote.")
    parser.add_argument("--repo-root", default=".", help="Path inside the local repository used for repo resolution.")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=github_api_core.TerminalArgumentParser)

    pulls = sub.add_parser("pulls")
    pulls.add_argument("--state", choices=("open", "closed", "all"), default="open")
    pulls.add_argument("--limit", type=int, default=20)
    pulls.add_argument("--head-branch")

    checks = sub.add_parser("pull-checks")
    checks.add_argument("number", type=int)

    issues = sub.add_parser("issues")
    issues.add_argument("--state", choices=("open", "closed", "all"), default="open")
    issues.add_argument("--limit", type=int, default=30)

    runs = sub.add_parser("workflow-runs")
    runs.add_argument("--branch")
    runs.add_argument("--limit", type=int, default=10)

    run = sub.add_parser("workflow-run")
    run.add_argument("run_id", type=int)

    jobs = sub.add_parser("workflow-jobs")
    jobs.add_argument("run_id", type=int)

    log = sub.add_parser("job-log")
    log.add_argument("job_id", type=int)

    security_status_parser = sub.add_parser("secret-scanning-status")
    security_status_parser.add_argument("--limit", type=int, default=100)

    sub.add_parser("repository")
    return parser.parse_args()


def main() -> int:
    try:
        args = parse_args()
    except github_api_core.ArgumentParsingError as exc:
        failure = github_api_core.FailureDetail(
            cause="validation_error",
            message=str(exc),
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            failed_step="argument_parsing",
        )
        return github_api_core.emit_terminal(
            github_api_core.terminal_failure(
                failure,
                operation="github.read.unknown",
                expected_actor=EXPECTED_ACTOR,
                transport="rest_api",
                bucket="rest_core",
                exit_code=2,
            ),
            stderr_message=f"error: {exc}",
        )

    operation = (
        "github.read.redacted_secret_scanning_status"
        if args.command == "secret-scanning-status"
        else f"github.read.{args.command.replace('-', '_')}"
    )
    gh_prefix_args = automation_only_gh_prefix_args() if args.command == "secret-scanning-status" else None
    reader = GitHubReader(
        gh_cmd=args.gh,
        expected_actor=EXPECTED_ACTOR,
        operation=operation,
        gh_prefix_args=gh_prefix_args,
        strict_actor=args.command == "secret-scanning-status",
    )
    try:
        repo_root = pathlib.Path(args.repo_root).resolve()
        repo = resolve_repo(repo_root, args.repo)
        if args.command == "pulls":
            data = list_pull_requests(
                reader,
                repo,
                state=args.state,
                limit=args.limit,
                head_branch=args.head_branch,
            )
        elif args.command == "pull-checks":
            data = pull_request_checks(reader, repo, args.number)
        elif args.command == "issues":
            data = list_issues(reader, repo, state=args.state, limit=args.limit)
        elif args.command == "workflow-runs":
            data = list_workflow_runs(reader, repo, branch=args.branch, limit=args.limit)
        elif args.command == "workflow-run":
            data = workflow_run(reader, repo, args.run_id)
        elif args.command == "workflow-jobs":
            data = workflow_jobs(reader, repo, args.run_id)
        elif args.command == "job-log":
            data = job_log(reader, repo, args.job_id)
        elif args.command == "secret-scanning-status":
            data = redacted_secret_scanning_status(reader, repo, limit=args.limit)
        else:
            data = repository(reader, repo)
    except GitHubReadError as exc:
        result = exc.result
        failure = result.failure or github_api_core.FailureDetail(
            cause="helper_error",
            message=str(exc),
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
        )
        return github_api_core.emit_terminal(
            github_api_core.terminal_failure(
                failure,
                operation=operation,
                payload={"diagnostics": exc.diagnostics},
                actor=result.actor,
                expected_actor=result.expected_actor,
                host=result.host,
                transport="rest_api",
                bucket=result.bucket or "rest_core",
                status=result.status,
                request_id=result.request_id,
                rate_limit=result.rate_limit,
                completed_steps=result.completed_steps,
                failed_step=result.failed_step,
                error=str(exc),
            ),
            stderr_message=f"error: {exc}",
        )
    except ValueError as exc:
        failure = github_api_core.FailureDetail(
            cause="validation_error",
            message=str(exc),
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            completed_steps=list(reader.completed_steps),
            failed_step="input_validation",
        )
        return github_api_core.emit_terminal(
            github_api_core.terminal_failure(
                failure,
                operation=operation,
                payload={"diagnostics": reader.diagnostics()},
                expected_actor=EXPECTED_ACTOR,
                transport="rest_api",
                bucket="rest_core",
                exit_code=2,
                completed_steps=reader.completed_steps,
                failed_step="input_validation",
            ),
            stderr_message=f"error: {exc}",
        )
    except GitHubReadShapeError as exc:
        failure = github_api_core.FailureDetail(
            cause="validation_error",
            message=str(exc),
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            completed_steps=list(reader.completed_steps),
            failed_step="response_validation",
        )
        return github_api_core.emit_terminal(
            github_api_core.terminal_failure(
                failure,
                operation=operation,
                payload={"diagnostics": reader.diagnostics()},
                expected_actor=EXPECTED_ACTOR,
                transport="rest_api",
                bucket="rest_core",
                exit_code=2,
                completed_steps=reader.completed_steps,
                failed_step="response_validation",
            ),
            stderr_message=f"error: {exc}",
        )

    last_result = reader.last_result
    return github_api_core.emit_terminal(
        github_api_core.terminal_success(
            {"repo": repo, "data": data, "diagnostics": reader.diagnostics()},
            operation=operation,
            actor=reader.actor,
            expected_actor=EXPECTED_ACTOR,
            host=last_result.host if last_result else github_api_core.DEFAULT_HOST,
            transport="rest_api",
            bucket="rest_core",
            status=last_result.status if last_result else 0,
            request_id=last_result.request_id if last_result else None,
            rate_limit=last_result.rate_limit if last_result else None,
            completed_steps=reader.completed_steps,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
