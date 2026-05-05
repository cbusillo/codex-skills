#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from shutil import which
from typing import Any, Iterable, Sequence

FAILURE_VALUES = {"failure", "error", "cancelled", "timed_out", "action_required"}
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
LOG_PENDING_MARKERS = (
    "still in progress",
    "log will be available when it is complete",
)


def main() -> int:
    args = parse_args()
    repo_root = find_git_root(Path(args.repo))
    if repo_root is None:
        print("error: not inside a git repository", file=sys.stderr)
        return 1

    if which("gh") is None:
        print("error: gh is not installed or not on PATH", file=sys.stderr)
        return 1

    auth_result = run_gh(["auth", "status"], repo_root)
    if auth_result.returncode != 0:
        print((auth_result.stderr or auth_result.stdout or "gh auth status failed").strip(), file=sys.stderr)
        return 1

    pr = resolve_pr(args.pr, repo_root)
    if pr is None:
        return 1

    checks = fetch_checks(pr, repo_root)
    if checks is None:
        return 1

    analyzed = [analyze_check(check, repo_root, args.max_lines, args.context) for check in checks]
    interesting = [item for item in analyzed if item["classification"] in {"failing", "pending", "external"}]

    payload = {
        "pr": pr,
        "failingCount": sum(1 for item in analyzed if item["classification"] == "failing"),
        "pendingCount": sum(1 for item in analyzed if item["classification"] == "pending"),
        "externalCount": sum(1 for item in analyzed if item["classification"] == "external"),
        "checks": interesting if args.only_interesting else analyzed,
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        render(payload)

    return 1 if payload["failingCount"] else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diagnose GitHub PR checks and extract concise GitHub Actions failure snippets.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--repo", default=".", help="Path inside the target git repository.")
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


class CommandResult:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def run_command(command: Sequence[str], cwd: Path, *, text: bool = True) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=text)


def run_gh(args: Sequence[str], cwd: Path) -> CommandResult:
    process = run_command(["gh", *args], cwd=cwd)
    assert isinstance(process.stdout, str)
    assert isinstance(process.stderr, str)
    return CommandResult(process.returncode, process.stdout, process.stderr)


def run_gh_raw(args: Sequence[str], cwd: Path) -> tuple[int, bytes, str]:
    process = run_command(["gh", *args], cwd=cwd, text=False)
    assert isinstance(process.stdout, bytes)
    assert isinstance(process.stderr, bytes)
    return process.returncode, process.stdout, process.stderr.decode(errors="replace")


def find_git_root(start: Path) -> Path | None:
    process = run_command(["git", "rev-parse", "--show-toplevel"], cwd=start)
    if process.returncode != 0:
        return None
    assert isinstance(process.stdout, str)
    return Path(process.stdout.strip())


def resolve_pr(pr: str | None, repo_root: Path) -> str | None:
    if pr:
        return pr
    result = run_gh(["pr", "view", "--json", "number"], repo_root)
    if result.returncode != 0:
        print((result.stderr or result.stdout or "unable to resolve current branch PR").strip(), file=sys.stderr)
        return None
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        print("error: unable to parse gh pr view JSON", file=sys.stderr)
        return None
    number = data.get("number")
    if not number:
        print("error: no PR found for current branch", file=sys.stderr)
        return None
    return str(number)


def fetch_checks(pr: str, repo_root: Path) -> list[dict[str, Any]] | None:
    field_sets = [
        ["name", "state", "conclusion", "detailsUrl", "startedAt", "completedAt"],
        ["name", "state", "bucket", "link", "startedAt", "completedAt", "workflow"],
    ]
    last_message = ""
    for fields in field_sets:
        result = run_gh(["pr", "checks", pr, "--json", ",".join(fields)], repo_root)
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout or "[]")
            except json.JSONDecodeError:
                print("error: unable to parse gh pr checks JSON", file=sys.stderr)
                return None
            if isinstance(data, list):
                return data
            print("error: unexpected gh pr checks JSON shape", file=sys.stderr)
            return None
        last_message = (result.stderr or result.stdout or "gh pr checks failed").strip()
    print(last_message, file=sys.stderr)
    return None


def analyze_check(check: dict[str, Any], repo_root: Path, max_lines: int, context: int) -> dict[str, Any]:
    name = str(check.get("name") or "")
    state = normalize(check.get("state") or check.get("status"))
    conclusion = normalize(check.get("conclusion"))
    bucket = normalize(check.get("bucket"))
    details_url = str(check.get("detailsUrl") or check.get("link") or "")
    run_id = extract_run_id(details_url)
    job_id = extract_job_id(details_url)

    item: dict[str, Any] = {
        "name": name,
        "state": state,
        "conclusion": conclusion,
        "bucket": bucket,
        "detailsUrl": details_url,
        "runId": run_id,
        "jobId": job_id,
    }

    if conclusion in FAILURE_VALUES or state in FAILURE_VALUES or bucket in FAILURE_BUCKETS:
        item["classification"] = "failing"
    elif state in PENDING_VALUES:
        item["classification"] = "pending"
    else:
        item["classification"] = "passing"
        return item

    if not run_id:
        item["classification"] = "external"
        item["note"] = "No GitHub Actions run id was found in the details URL."
        return item

    metadata = fetch_run_metadata(run_id, repo_root)
    if metadata:
        item["run"] = metadata

    if item["classification"] == "pending":
        item["note"] = "Check is still pending; logs may not be available yet."
        return item

    log_text, log_error, log_status = fetch_logs(run_id, job_id, repo_root)
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


def extract_run_id(url: str) -> str | None:
    for pattern in (r"/actions/runs/(\d+)", r"/runs/(\d+)"):
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def extract_job_id(url: str) -> str | None:
    for pattern in (r"/actions/runs/\d+/job/(\d+)", r"/job/(\d+)"):
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def fetch_run_metadata(run_id: str, repo_root: Path) -> dict[str, Any] | None:
    fields = "conclusion,status,workflowName,name,event,headBranch,headSha,url"
    result = run_gh(["run", "view", run_id, "--json", fields], repo_root)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def fetch_logs(run_id: str, job_id: str | None, repo_root: Path) -> tuple[str, str, str]:
    failed_result = run_gh(["run", "view", run_id, "--log-failed"], repo_root)
    if failed_result.returncode == 0 and failed_result.stdout:
        return failed_result.stdout, "", "ok"

    full_result = run_gh(["run", "view", run_id, "--log"], repo_root)
    if full_result.returncode == 0 and full_result.stdout:
        return full_result.stdout, "", "ok"

    error = (failed_result.stderr or failed_result.stdout or full_result.stderr or full_result.stdout or "").strip()
    if job_id and is_pending_log_message(error):
        job_log, job_error = fetch_job_log(job_id, repo_root)
        if job_log:
            return job_log, "", "ok"
        if job_error and is_pending_log_message(job_error):
            return "", job_error, "pending"
        return "", job_error or error, "error"
    if is_pending_log_message(error):
        return "", error, "pending"
    return "", error or "unable to fetch logs", "error"


def fetch_job_log(job_id: str, repo_root: Path) -> tuple[str, str]:
    repo_slug = fetch_repo_slug(repo_root)
    if not repo_slug:
        return "", "unable to resolve repository slug for job log lookup"
    returncode, stdout, stderr = run_gh_raw(["api", f"/repos/{repo_slug}/actions/jobs/{job_id}/logs"], repo_root)
    if returncode != 0:
        return "", (stderr or stdout.decode(errors="replace") or "gh api job logs failed").strip()
    if stdout.startswith(b"PK"):
        return "", "job logs returned a zip archive; unable to parse directly"
    return stdout.decode(errors="replace"), ""


def fetch_repo_slug(repo_root: Path) -> str | None:
    result = run_gh(["repo", "view", "--json", "nameWithOwner"], repo_root)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    value = data.get("nameWithOwner")
    return str(value) if value else None


def is_pending_log_message(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in LOG_PENDING_MARKERS)


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


def render(payload: dict[str, Any]) -> None:
    print(
        f"PR {payload['pr']}: {payload['failingCount']} failing, "
        f"{payload['pendingCount']} pending, {payload['externalCount']} external checks."
    )
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
