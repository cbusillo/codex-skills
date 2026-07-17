#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from shutil import which
from typing import Any, Iterable, Optional, Sequence

import github_read as github_read_core


FAILURE_VALUES = {"failure", "error", "cancelled", "timed_out", "action_required", "startup_failure"}
FAILURE_BUCKETS = {"fail"}
PENDING_VALUES = {"pending", "queued", "in_progress", "waiting", "requested"}
FAILURE_MARKERS = (
    "error",
    "failed",
    "failure",
    "traceback",
    "exception",
    "assert",
    "panic",
    "fatal",
    "timeout",
    "segmentation fault",
)
SCRIPT_DIR = Path(__file__).resolve().parent
GH_COMMAND = os.environ.get("GITHUB_CI_DIAGNOSE_GH") or str(SCRIPT_DIR / "gh-with-env-token")
EXPECTED_ACTOR = os.environ.get("GH_WITH_ENV_TOKEN_EXPECTED_LOGIN") or "shiny-code-bot"


def main() -> int:
    args = parse_args()
    repo_root = find_git_root(Path(args.repo))
    if repo_root is None:
        print("error: not inside a git repository", file=sys.stderr)
        return 1

    if Path(GH_COMMAND).name == "gh" and which("gh") is None:
        print("error: gh is not installed or not on PATH", file=sys.stderr)
        return 1

    url_ref = parse_pr_url(args.pr)
    try:
        default_repo = url_ref[0] if url_ref else github_read_core.resolve_repo(repo_root, args.github_repo)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    reader = github_read_core.GitHubReader(
        gh_cmd=GH_COMMAND,
        expected_actor=EXPECTED_ACTOR,
        operation="github.ci_diagnose",
    )
    try:
        repo, pr_number, display_pr = resolve_pr(args.pr, default_repo, repo_root, reader)
        checks_payload = github_read_core.pull_request_checks(reader, repo, pr_number)
    except github_read_core.GitHubReadError as exc:
        reader.mark_degraded("pullRequestChecks", exc.result.failure.cause if exc.result.failure else "read_failed", str(exc))
        return emit_failure(args, args.pr or "current", reader, str(exc))
    except (github_read_core.GitHubReadShapeError, ValueError) as exc:
        reader.mark_degraded("pullRequestChecks", "invalid_response", str(exc))
        return emit_failure(args, args.pr or "current", reader, str(exc))

    client = DiagnosisClient(reader, repo)
    checks = combined_checks(checks_payload)
    analyzed = [analyze_check(check, client, args.max_lines, args.context) for check in checks]
    interesting = [item for item in analyzed if item["classification"] in {"failing", "pending", "external"}]
    summary = checks_payload.get("summary") or {}
    availability = summary.get("availability") or {}
    counts_complete = bool(summary.get("countsComplete", True))
    checks_available = bool(availability.get("checkRuns", True) or availability.get("commitStatuses", True))
    failing_count = sum(1 for item in analyzed if item["classification"] == "failing") if checks_available else None
    pending_count = sum(1 for item in analyzed if item["classification"] == "pending") if checks_available else None
    external_count = sum(1 for item in analyzed if item["classification"] == "external") if checks_available else None

    payload = {
        "pr": display_pr,
        "repo": repo,
        "failingCount": failing_count,
        "pendingCount": pending_count,
        "externalCount": external_count,
        "countsComplete": counts_complete,
        "unavailableCheckComponents": summary.get("unavailableComponents") or [],
        "actor": reader.actor,
        "expectedActor": EXPECTED_ACTOR,
        "checks": interesting if args.only_interesting else analyzed,
        "diagnostics": reader.diagnostics(),
    }
    if not checks_available:
        payload["error"] = "PR check data is unavailable; counts are unknown."

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        render(payload)

    return 1 if failing_count is None or failing_count else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diagnose GitHub PR checks and extract concise GitHub Actions failure snippets.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--repo", default=".", help="Path inside the target git repository.")
    parser.add_argument("--github-repo", help="Repository in OWNER/REPO form. Defaults to the origin remote.")
    parser.add_argument("--pr", default=None, help="PR number or URL. Defaults to the current branch PR.")
    parser.add_argument("--json", action="store_true", help="Emit JSON for agent/tool consumption.")
    parser.add_argument("--max-lines", type=int, default=140, help="Maximum snippet/tail lines per failing check.")
    parser.add_argument("--context", type=int, default=30, help="Context lines around the detected failure marker.")
    parser.add_argument(
        "--all-checks",
        action="store_false",
        dest="only_interesting",
        help="Include passing checks in output.",
    )
    parser.set_defaults(only_interesting=True)
    return parser.parse_args()


def run_command(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True)


def find_git_root(start: Path) -> Path | None:
    process = run_command(["git", "rev-parse", "--show-toplevel"], cwd=start)
    if process.returncode != 0:
        return None
    return Path(process.stdout.strip())


def current_branch(repo_root: Path) -> str:
    process = run_command(["git", "branch", "--show-current"], cwd=repo_root)
    if process.returncode != 0 or not process.stdout.strip():
        raise ValueError("current branch is detached; pass --pr")
    return process.stdout.strip()


def resolve_pr(
    value: Optional[str],
    default_repo: str,
    repo_root: Path,
    reader: github_read_core.GitHubReader,
) -> tuple[str, int, str]:
    if value:
        if value.isdigit():
            return default_repo, int(value), value
        url_ref = parse_pr_url(value)
        if url_ref:
            return url_ref[0], url_ref[1], value
        raise ValueError("--pr must be a PR number or URL")

    branch = current_branch(repo_root)
    try:
        head_repo = github_read_core.resolve_repo_from_remote(repo_root, "origin")
    except ValueError:
        head_repo = default_repo
    candidate_repos = [default_repo]
    try:
        upstream_repo = github_read_core.resolve_repo_from_remote(repo_root, "upstream")
    except ValueError:
        upstream_repo = None
    if upstream_repo and upstream_repo not in candidate_repos:
        candidate_repos.append(upstream_repo)
    for candidate_repo in candidate_repos:
        candidate_pulls = github_read_core.list_pull_requests(
            reader,
            candidate_repo,
            head_branch=branch,
            head_owner=head_repo.split("/", 1)[0],
            limit=2,
        )
        if len(candidate_pulls) > 1:
            raise ValueError(f"multiple open PRs found for current branch {branch}; pass --pr")
        if len(candidate_pulls) == 1:
            number = candidate_pulls[0].get("number")
            if not isinstance(number, int):
                raise github_read_core.GitHubReadShapeError("GitHub pull request lookup did not return a number")
            return candidate_repo, number, str(number)
    raise ValueError(f"no open PR found for current branch {branch}")


def parse_pr_url(value: Optional[str]) -> Optional[tuple[str, int]]:
    if not value:
        return None
    match = re.search(r"^(?:https?://)?[^/]+/([^/]+)/([^/]+)/(?:pull|pulls)/(\d+)(?:[/?#].*)?$", value)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}", int(match.group(3))


def combined_checks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for item in payload.get("checkRuns") or []:
        if not isinstance(item, dict):
            continue
        checks.append({
            "name": item.get("name"),
            "state": item.get("status"),
            "conclusion": item.get("conclusion"),
            "bucket": "",
            "detailsUrl": item.get("detailsUrl"),
            "startedAt": item.get("startedAt"),
            "completedAt": item.get("completedAt"),
            "runId": item.get("runId"),
            "jobId": item.get("jobId"),
            "source": "check_run",
        })
    for item in payload.get("statuses") or []:
        if not isinstance(item, dict):
            continue
        checks.append({
            "name": item.get("context"),
            "state": item.get("state"),
            "conclusion": item.get("state"),
            "bucket": "",
            "detailsUrl": item.get("targetUrl"),
            "startedAt": item.get("createdAt"),
            "completedAt": item.get("updatedAt"),
            "runId": None,
            "jobId": None,
            "source": "commit_status",
        })
    return checks


class DiagnosisClient:
    def __init__(self, reader: github_read_core.GitHubReader, repo: str) -> None:
        self.reader = reader
        self.repo = repo
        self.run_cache: dict[int, Optional[dict[str, Any]]] = {}
        self.jobs_cache: dict[int, Optional[list[dict[str, Any]]]] = {}
        self.log_cache: dict[int, tuple[str, str, str]] = {}

    def run_metadata(self, run_id: int) -> Optional[dict[str, Any]]:
        if run_id in self.run_cache:
            return self.run_cache[run_id]
        try:
            value = github_read_core.workflow_run(self.reader, self.repo, run_id)
        except github_read_core.GitHubReadError as exc:
            code = exc.result.failure.cause if exc.result.failure else "read_failed"
            self.reader.mark_degraded("workflowRun", code, str(exc))
            value = None
        except github_read_core.GitHubReadShapeError as exc:
            self.reader.mark_degraded("workflowRun", "invalid_response", str(exc))
            value = None
        self.run_cache[run_id] = value
        return value

    def jobs(self, run_id: int) -> Optional[list[dict[str, Any]]]:
        if run_id in self.jobs_cache:
            return self.jobs_cache[run_id]
        try:
            value = github_read_core.workflow_jobs(self.reader, self.repo, run_id)
        except github_read_core.GitHubReadError as exc:
            code = exc.result.failure.cause if exc.result.failure else "read_failed"
            self.reader.mark_degraded("workflowJobs", code, str(exc))
            value = None
        except github_read_core.GitHubReadShapeError as exc:
            self.reader.mark_degraded("workflowJobs", "invalid_response", str(exc))
            value = None
        self.jobs_cache[run_id] = value
        return value

    def resolve_job_id(self, run_id: int, check_name: str) -> tuple[Optional[int], str]:
        jobs = self.jobs(run_id)
        if jobs is None:
            return None, "Workflow jobs are unavailable."
        matches = [job for job in jobs if normalize(job.get("name")) == normalize(check_name)]
        if len(matches) == 1 and isinstance(matches[0].get("id"), int):
            return int(matches[0]["id"]), ""
        if len(matches) > 1:
            message = f"Multiple workflow jobs matched check name {check_name!r}."
            self.reader.mark_degraded("workflowJobs", "ambiguous_job_mapping", message)
            return None, message
        message = f"No workflow job matched check name {check_name!r}."
        self.reader.mark_degraded("workflowJobs", "job_not_found", message)
        return None, message

    def log(self, job_id: int, *, run_pending: bool) -> tuple[str, str, str]:
        if job_id in self.log_cache:
            return self.log_cache[job_id]
        try:
            value = (github_read_core.job_log(self.reader, self.repo, job_id), "", "ok")
        except github_read_core.GitHubReadError as exc:
            code = exc.result.failure.cause if exc.result.failure else "read_failed"
            status = "pending" if run_pending and exc.result.status in {404, 409} else "error"
            self.reader.mark_degraded("workflowLog", code, str(exc))
            value = ("", str(exc), status)
        except github_read_core.GitHubReadShapeError as exc:
            self.reader.mark_degraded("workflowLog", "unsupported_log_response", str(exc))
            value = ("", str(exc), "error")
        self.log_cache[job_id] = value
        return value


def analyze_check(check: dict[str, Any], client: DiagnosisClient, max_lines: int, context: int) -> dict[str, Any]:
    name = str(check.get("name") or "")
    state = normalize(check.get("state") or check.get("status"))
    conclusion = normalize(check.get("conclusion"))
    bucket = normalize(check.get("bucket"))
    details_url = str(check.get("detailsUrl") or check.get("link") or "")
    run_id = int(check["runId"]) if isinstance(check.get("runId"), int) else extract_run_id(details_url)
    job_id = int(check["jobId"]) if isinstance(check.get("jobId"), int) else extract_job_id(details_url)

    item: dict[str, Any] = {
        "name": name,
        "state": state,
        "conclusion": conclusion,
        "bucket": bucket,
        "detailsUrl": details_url,
        "runId": str(run_id) if run_id is not None else None,
        "jobId": str(job_id) if job_id is not None else None,
        "source": check.get("source"),
    }

    if conclusion in FAILURE_VALUES or state in FAILURE_VALUES or bucket in FAILURE_BUCKETS:
        item["classification"] = "failing"
    elif state in PENDING_VALUES:
        item["classification"] = "pending"
    else:
        item["classification"] = "passing"
        return item

    if run_id is None:
        item["classification"] = "external"
        item["note"] = "No GitHub Actions run id was found in the details URL."
        return item

    metadata = client.run_metadata(run_id)
    if metadata:
        item["run"] = metadata

    if item["classification"] == "pending":
        item["note"] = "Check is still pending; logs may not be available yet."
        return item

    if job_id is None:
        job_id, job_error = client.resolve_job_id(run_id, name)
        if job_id is None:
            item["error"] = job_error
            return item
        item["jobId"] = str(job_id)

    run_pending = normalize((metadata or {}).get("status")) in PENDING_VALUES
    log_text, log_error, log_status = client.log(job_id, run_pending=run_pending)
    if log_status == "pending":
        item["note"] = log_error or "Logs are not available yet."
        return item
    if log_error:
        item["error"] = log_error
        return item

    item["failureSnippet"] = extract_failure_snippet(log_text, max_lines=max(1, max_lines), context=max(1, context))
    item["logTail"] = tail_lines(log_text, max_lines=max(1, max_lines))
    return item


def normalize(value: Any) -> str:
    return "" if value is None else str(value).strip().lower()


def extract_run_id(url: str) -> Optional[int]:
    for pattern in (r"/actions/runs/(\d+)", r"/runs/(\d+)"):
        match = re.search(pattern, url)
        if match:
            return int(match.group(1))
    return None


def extract_job_id(url: str) -> Optional[int]:
    for pattern in (r"/actions/runs/\d+/job/(\d+)", r"/job/(\d+)"):
        match = re.search(pattern, url)
        if match:
            return int(match.group(1))
    return None


def extract_failure_snippet(log_text: str, max_lines: int, context: int) -> str:
    lines = log_text.splitlines()
    if not lines:
        return ""
    marker_index = find_failure_index(lines)
    if marker_index is None:
        return "\n".join(lines[-max_lines:])
    start = max(0, marker_index - context)
    end = min(len(lines), marker_index + context + 1)
    window = lines[start:end]
    if len(window) > max_lines:
        window = window[-max_lines:]
    return "\n".join(window)


def find_failure_index(lines: Sequence[str]) -> int | None:
    for index in range(len(lines) - 1, -1, -1):
        lowered = lines[index].lower()
        if any(marker in lowered for marker in FAILURE_MARKERS):
            return index
    return None


def tail_lines(text: str, max_lines: int) -> str:
    return "\n".join(text.splitlines()[-max_lines:])


def emit_failure(
    args: argparse.Namespace,
    pr: str,
    reader: github_read_core.GitHubReader,
    message: str,
) -> int:
    payload = {
        "pr": pr,
        "failingCount": None,
        "pendingCount": None,
        "externalCount": None,
        "countsComplete": False,
        "unavailableCheckComponents": ["pullRequestChecks"],
        "checks": [],
        "error": message,
        "diagnostics": reader.diagnostics(),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"error: {message}", file=sys.stderr)
        render(payload)
    return 1


def render(payload: dict[str, Any]) -> None:
    def count(value: Any) -> str:
        return "unknown" if value is None else str(value)

    print(
        f"PR {payload['pr']}: {count(payload['failingCount'])} failing, "
        f"{count(payload['pendingCount'])} pending, {count(payload['externalCount'])} external checks."
    )
    if not payload.get("countsComplete", True):
        unavailable = ", ".join(payload.get("unavailableCheckComponents") or []) or "check data"
        print(f"Check counts are partial or unknown: {unavailable} unavailable.")
    diagnostics = payload.get("diagnostics") or {}
    if diagnostics.get("degraded"):
        components = ", ".join(diagnostics.get("degradedComponents") or []) or "GitHub metadata"
        print(f"Diagnosis degraded: {components} unavailable or incomplete.")
    checks: Iterable[dict[str, Any]] = payload["checks"]
    for check in checks:
        print("-" * 72)
        print(f"Check: {check.get('name', '')}")
        print(f"Classification: {check.get('classification', '')}")
        if check.get("detailsUrl"):
            print(f"Details: {check['detailsUrl']}")
        if check.get("runId"):
            print(f"Run ID: {check['runId']}")
        run = check.get("run") or {}
        if run:
            workflow = run.get("workflowName") or run.get("name") or ""
            sha = (run.get("headSha") or "")[:12]
            branch = run.get("headBranch") or ""
            status = run.get("conclusion") or run.get("status") or ""
            print(f"Workflow: {workflow} ({status})")
            if branch or sha:
                print(f"Branch/SHA: {branch} {sha}")
            if run.get("url"):
                print(f"Run URL: {run['url']}")
        if check.get("note"):
            print(f"Note: {check['note']}")
        if check.get("error"):
            print(f"Log error: {check['error']}")
        snippet = check.get("failureSnippet") or ""
        if snippet:
            print("Failure snippet:")
            print(indent(snippet))
    print("-" * 72)


def indent(text: str) -> str:
    return "\n".join(f"  {line}" for line in text.splitlines())


if __name__ == "__main__":
    raise SystemExit(main())
