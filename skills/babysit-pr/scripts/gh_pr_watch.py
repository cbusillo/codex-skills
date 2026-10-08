#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Watch GitHub PR CI and review activity for PR babysitting workflows."""

import argparse
import base64
import fcntl
from contextlib import contextmanager
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_GH = SCRIPT_DIR.parent.parent / "github" / "scripts" / "gh-with-env-token"
GH_COMMAND = os.environ.get("GH_PR_WATCH_GH") or str(DEFAULT_GH)
COMMAND_TIMEOUT_SECONDS = 60.0
LOCK_TIMEOUT_SECONDS = 60.0
DEFAULT_PR_HELPER = SCRIPT_DIR.parent.parent / "github" / "scripts" / "gh-pr.py"
PR_HELPER = os.environ.get("GH_PR_WATCH_PR_HELPER") or str(DEFAULT_PR_HELPER)
DEFAULT_OWNER_REVIEW_HELPER = SCRIPT_DIR.parent.parent / "launchplane" / "scripts" / "launchplane-owner-review.py"
OWNER_REVIEW_HELPER = os.environ.get("GH_PR_WATCH_OWNER_REVIEW_HELPER") or str(DEFAULT_OWNER_REVIEW_HELPER)
IDENTITY_SCRIPT_DIR = SCRIPT_DIR.parent.parent / "github" / "scripts"
if str(IDENTITY_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(IDENTITY_SCRIPT_DIR))
import github_api
import github_identity
import github_read

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
    "github-advanced-security",
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


MERGE_CONFLICT_OR_BLOCKING_STATES = {
    "BEHIND",
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
REVIEW_DECISION_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner
    pullRequest(number: $number) {
      number
      url
      headRefOid
      baseRefName
      isDraft
      state
      reviewDecision
      mergeStateStatus
    }
  }
}
""".strip()


REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    nameWithOwner
    pullRequest(number: $number) {
      number url headRefOid
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated
          comments(first: 100) {
            pageInfo { hasNextPage }
            nodes { databaseId commit { oid } }
          }
        }
      }
    }
  }
}
""".strip()


def fetch_review_threads(pr, comments, reader):
    """Re-read resolution for actionable inline bots, even after comments are seen."""
    ids = {item["id"] for item in comments if is_actionable_review_bot_login(item["author"])}
    evidence = {"status": "available", "head_sha": pr["head_sha"], "threads": []}
    if not ids or pr["closed"] or pr["merged"]:
        return evidence
    if reader is None:
        reader = watcher_reader()
    owner, name = pr["repo"].split("/", 1)
    cursor = None
    matched = set()
    for _ in range(10):
        result = reader.graphql_json(
            REVIEW_THREADS_QUERY,
            {"owner": owner, "name": name, "number": pr["number"], "cursor": cursor},
            step="review_threads", operation="github.pr.review_threads",
            retry_policy=github_api.RetryPolicy(max_wait_seconds=2.0, max_attempts=1),
            deadline_at=time.time() + 2.0,
        )
        body = result.body if result.ok else None
        repository = body.get("data", {}).get("repository") if isinstance(body, dict) and isinstance(body.get("data"), dict) else None
        item = repository.get("pullRequest") if isinstance(repository, dict) else None
        if (
            not isinstance(body, dict) or body.get("errors")
            or not isinstance(item, dict)
            or str(repository.get("nameWithOwner") or "").casefold() != pr["repo"].casefold()
            or item.get("number") != pr["number"] or item.get("url") != pr["url"]
            or item.get("headRefOid") != pr["head_sha"]
            or any(reason.get("component") == "actor" for reason in reader.degraded_reasons)
        ):
            break
        connection = item.get("reviewThreads")
        if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
            break
        for thread in connection["nodes"]:
            if not isinstance(thread, dict) or type(thread.get("isResolved")) is not bool or type(thread.get("isOutdated")) is not bool or not thread.get("id"):
                break
            thread_comments = thread.get("comments")
            if not isinstance(thread_comments, dict) or not isinstance(thread_comments.get("nodes"), list) or thread_comments.get("pageInfo", {}).get("hasNextPage") is not False:
                break
            thread_evidence = {
                "id": thread["id"], "is_resolved": thread["isResolved"],
                "is_outdated": thread["isOutdated"], "head_sha": pr["head_sha"], "comments": [],
            }
            for comment in thread_comments["nodes"]:
                if not isinstance(comment, dict):
                    break
                comment_id = str(comment.get("databaseId") or "")
                if comment_id in ids:
                    matched.add(comment_id)
                    commit = comment.get("commit")
                    commit_sha = commit.get("oid") if isinstance(commit, dict) else None
                    thread_evidence["comments"].append({
                        "comment_id": comment_id, "commit_sha": commit_sha,
                        "matches_current_head": commit_sha == pr["head_sha"],
                    })
            else:
                if thread_evidence["comments"]:
                    evidence["threads"].append(thread_evidence)
                continue
            break
        else:
            page = connection.get("pageInfo", {})
            if page.get("hasNextPage") is False:
                if matched == ids:
                    return evidence
                break
            next_cursor = page.get("endCursor")
            if page.get("hasNextPage") is True and next_cursor and next_cursor != cursor:
                cursor = next_cursor
                continue
        break
    evidence["status"] = "unknown"
    reader.mark_degraded("review_threads", "incomplete_review_threads", "Review-thread resolution could not be proven for the current head")
    return evidence


class GhCommandError(RuntimeError):
    pass


class GhCommandNotSent(GhCommandError):
    pass


class StateLockTimeout(RuntimeError):
    def __init__(self, path):
        super().__init__(f"State lock timed out: {path}; preserve state and retry after its holder exits")
        self.path = path


class PrHelperReadError(GhCommandError):
    def __init__(self, message, payload):
        super().__init__(message)
        self.payload = github_api.redact_body(payload)


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Normalize PR/CI/review state for PR babysitting and optionally "
            "trigger flaky reruns."
        )
    )
    parser.add_argument("--pr", default="auto", help="auto, PR number, or PR URL")
    parser.add_argument("--repo", help="Optional OWNER/REPO override")
    parser.add_argument("--poll-seconds", type=int, default=60, help="Active watch poll interval")
    parser.add_argument("--green-poll-seconds", type=int, default=300, help="Quiet green PR poll interval")
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

    if args.poll_seconds <= 0 or args.green_poll_seconds <= 0:
        parser.error("poll intervals must be > 0")
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


@contextmanager
def command_signal_cleanup():
    """Let catchable parent termination use the same cleanup as interruption."""
    previous = {}
    terminating = False
    def terminate(signum, _frame):
        nonlocal terminating
        if terminating:
            return
        terminating = True
        raise SystemExit(128 + signum)
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGHUP):
            handler = signal.getsignal(signum)
            if handler != signal.SIG_IGN:
                previous[signum] = signal.signal(signum, terminate)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def gh_text(args, repo=None):
    cmd = [GH_COMMAND]
    # `gh api` does not accept `-R/--repo` on all gh versions. The watcher's
    # API calls use explicit endpoints (e.g. repos/{owner}/{repo}/...), so the
    # repo flag is unnecessary there.
    if repo and (not args or args[0] != "api"):
        cmd.extend(["-R", repo])
    cmd.extend(args)
    timeout = min(COMMAND_TIMEOUT_SECONDS, github_api.remaining_retry_timeout_seconds())
    if timeout <= 0:
        raise GhCommandNotSent("GitHub command deadline expired before launch; no write sent")
    env = os.environ.copy()
    receipt_nonce = secrets.token_hex(16)
    env["GH_WITH_ENV_TOKEN_RECEIPT_NONCE"] = receipt_nonce
    # Let managed preflight reads refuse before our process-group ceiling.
    # Reserve a short interval for the wrapper to report that refusal.
    env["GITHUB_RETRY_DEADLINE_AT"] = str(time.time() + timeout - min(2.0, timeout / 2))
    with command_signal_cleanup():
        return run_gh_command(cmd, env, timeout, receipt_nonce)


def run_gh_command(cmd, env, timeout, receipt_nonce):
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True, env=env)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except BaseException:
            # The auth wrapper has child processes. Kill the whole command group
            # so an inherited stdout pipe cannot keep communicate/our lock hung.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise
        if proc.returncode:
            lines = stderr.strip().splitlines()
            try:
                receipt = json.loads(lines[-1]) if lines else None
            except json.JSONDecodeError:
                receipt = None
            if GH_COMMAND == str(DEFAULT_GH) and receipt == {
                "schema_version": 1, "nonce": receipt_nonce, "write_outcome": "not_started",
            }:
                raise GhCommandNotSent("GitHub wrapper refused before sending the write: " + "\n".join(lines[:-1]))
            raise subprocess.CalledProcessError(proc.returncode, cmd, stdout, stderr)
    except subprocess.TimeoutExpired as err:
        raise GhCommandError("GitHub CLI command timed out; rerun outcome is unknown") from err
    except FileNotFoundError as err:
        raise GhCommandError("`gh` command not found") from err
    except subprocess.CalledProcessError as err:
        raise GhCommandError(_format_gh_error(cmd, err)) from err
    return stdout


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
    env["GITHUB_REQUEST_CALLER"] = os.environ.get("GITHUB_REQUEST_CALLER") or Path(__file__).name
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)

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
        detail = str(payload.get("error") or payload.get("recommended_next_action") or "")
        suffix = f": {detail}" if detail else ""
        raise PrHelperReadError(f"REST-first PR helper failed for {command}{suffix}", payload)
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
        "head_repository": str(data.get("headRepository") or ""),
        "base_branch": str(data.get("baseRefName") or ""),
        "merge_commit_sha": str(data.get("mergeCommitOid") or "") if merged else "",
        "state": state,
        "merged": merged,
        "closed": closed,
        "draft": data.get("draft") is True,
        "mergeable": normalize_mergeable(data.get("mergeable")),
        "merge_state_status": merge_state_status,
        "review_decision": str(review_decision or ""),
        "review_requirement": "unknown",
        "review_decision_source": "not_queried",
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


@contextmanager
def state_lock(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a") as lock_file:
        deadline = time.monotonic() + min(LOCK_TIMEOUT_SECONDS, github_api.remaining_retry_timeout_seconds())
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise StateLockTimeout(path)
                time.sleep(min(0.05, remaining))
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def save_state(path, state):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(payload)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def default_state_file_for(pr):
    root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state").expanduser()
    if not root.is_absolute():
        root = Path.home() / ".local" / "state"
    return root / "pr-babysit" / legacy_state_file_for(pr).name


def legacy_state_file_for(pr):
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


def apply_unfinished_workflow_runs(checks_summary, runs, head_sha):
    # A queued run has no check runs yet, so the check counts alone cannot see it.
    unfinished = sum(
        1
        for run in runs
        if isinstance(run, dict)
        and str(run.get("head_sha") or "") == head_sha
        and str(run.get("status") or "") != "completed"
    )
    return {
        **checks_summary,
        "unfinished_workflow_run_count": unfinished,
        "all_terminal": checks_summary["all_terminal"] and unfinished == 0,
    }


def watcher_reader():
    return github_read.GitHubReader(
        gh_cmd=GH_COMMAND,
        expected_actor=github_identity.automation_login(),
        operation="github.pr.watch",
        cache_enabled=True,
    )


def query_review_readiness(pr, reader):
    """Read review policy once, only after REST proves every other gate."""
    owner, name = pr["repo"].split("/", 1)
    retry_policy = github_api.RetryPolicy(max_wait_seconds=2.0, max_attempts=1)
    result = reader.graphql_json(
        REVIEW_DECISION_QUERY,
        {"owner": owner, "name": name, "number": pr["number"]},
        step="review_readiness",
        operation="github.pr.review_readiness",
        retry_policy=retry_policy,
        deadline_at=time.time() + 2.0,
    )
    diagnostic = reader.requests[-1] if reader.requests else {}
    if not result.ok:
        return {"status": "unknown", "requirement": "unknown", "source": "transport"}, diagnostic
    if any(
        reason.get("component") == "actor"
        for reason in getattr(reader, "degraded_reasons", [])
    ):
        return {"status": "unknown", "requirement": "unknown", "source": "transport"}, diagnostic
    body = result.body
    if not isinstance(body, dict) or body.get("errors"):
        reader.mark_degraded("review_readiness", "graphql_partial_error", "GraphQL review query returned errors")
        return {"status": "unknown", "requirement": "unknown", "source": "transport"}, diagnostic
    repository = body.get("data", {}).get("repository") if isinstance(body.get("data"), dict) else None
    item = repository.get("pullRequest") if isinstance(repository, dict) else None
    if not isinstance(repository, dict) or not isinstance(item, dict):
        reader.mark_degraded("review_readiness", "graphql_missing_pull_request", "GraphQL review query omitted the pull request")
        return {"status": "unknown", "requirement": "unknown", "source": "transport"}, diagnostic
    expected_repo = pr["repo"].casefold()
    if str(repository.get("nameWithOwner") or "").casefold() != expected_repo:
        reason = "GraphQL review query repository did not match the REST target"
        reader.mark_degraded("review_readiness", "graphql_repository_mismatch", reason)
        return {"status": "unknown", "requirement": "unknown", "source": "transport"}, diagnostic
    if (
        item.get("number") != pr["number"]
        or str(item.get("headRefOid") or "") != pr["head_sha"]
        or str(item.get("url") or "") != pr["url"]
    ):
        reason = "GraphQL review query PR number or head SHA did not match the REST target"
        reader.mark_degraded("review_readiness", "graphql_head_mismatch", reason)
        return {"status": "unknown", "requirement": "unknown", "source": "transport"}, diagnostic
    if item.get("baseRefName") != pr["base_branch"] or item.get("isDraft") is not pr["draft"]:
        reason = "GraphQL review query base branch or draft state did not match the REST target"
        reader.mark_degraded("review_readiness", "graphql_state_mismatch", reason)
        return {"status": "unknown", "requirement": "unknown", "source": "transport"}, diagnostic
    if str(item.get("state") or "").upper() != "OPEN":
        reason = "GraphQL review query state was not OPEN"
        reader.mark_degraded("review_readiness", "graphql_state_mismatch", reason)
        return {"status": "unknown", "requirement": "unknown", "source": "transport"}, diagnostic
    if "reviewDecision" not in item or "mergeStateStatus" not in item:
        reader.mark_degraded("review_readiness", "graphql_missing_field", "GraphQL review query omitted a required field")
        return {"status": "unknown", "requirement": "unknown", "source": "missing_field"}, diagnostic
    merge_state = str(item.get("mergeStateStatus") or "").upper()
    if merge_state not in KNOWN_MERGE_STATES:
        reader.mark_degraded("review_readiness", "graphql_unknown_merge_state", "GraphQL review query returned an unknown merge state")
        return {"status": "unknown", "requirement": "unknown", "source": "graphql"}, diagnostic
    decision = item.get("reviewDecision")
    if decision is None:
        requirement = "not_applicable" if merge_state == "CLEAN" else "unknown"
    else:
        decision = str(decision).upper()
        requirement = {
            "APPROVED": "satisfied",
            "CHANGES_REQUESTED": "changes_requested",
            "REVIEW_REQUIRED": "pending",
        }.get(decision, "unknown")
        if requirement == "unknown":
            reader.mark_degraded("review_readiness", "graphql_unknown_review_decision", "GraphQL review query returned an unknown review decision")
            return {"status": "unknown", "requirement": "unknown", "source": "graphql"}, diagnostic
    return {
        "status": "available",
        "requirement": requirement,
        "source": "graphql",
        "decision": decision,
        "merge_state_status": merge_state,
    }, diagnostic


def apply_review_readiness(pr, review):
    pr["review_requirement"] = review.get("requirement", "unknown")
    pr["review_decision_source"] = review.get("source", "transport")
    if review.get("status") == "available":
        pr["metadata_availability"]["review_decision"] = True
        decision = review.get("decision")
        pr["review_decision"] = decision
        if review.get("merge_state_status"):
            pr["merge_state_status"] = review["merge_state_status"]
    else:
        pr["metadata_availability"]["review_decision"] = False


def review_readiness_diagnostic(reader, review):
    """Keep the exact query evidence beside its normalized readiness result."""
    return {
        "request": reader.requests[-1] if reader.requests else None,
        "readiness": {
            "status": review.get("status"),
            "source": review.get("source"),
            "requirement": review.get("requirement"),
        },
        "degradedReasons": list(getattr(reader, "degraded_reasons", [])),
    }


def get_workflow_runs_for_sha(repo, head_sha, reader=None):
    if reader is not None:
        return reader.paged_json(
            f"/repos/{repo}/actions/runs", step_prefix="workflow_runs",
            params={"head_sha": head_sha}, collection_key="workflow_runs",
        )
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
                "run_attempt": run.get("run_attempt"),
                "workflow_name": run.get("name") or run.get("display_title") or "",
                "status": str(run.get("status") or ""),
                "conclusion": conclusion,
                "html_url": str(run.get("html_url") or ""),
            }
        )
    failed_runs.sort(key=lambda item: (str(item.get("workflow_name") or ""), str(item.get("run_id") or "")))
    return failed_runs


def get_jobs_for_run(repo, run_id, reader=None):
    if reader is not None:
        return reader.paged_json(
            f"/repos/{repo}/actions/runs/{run_id}/jobs", step_prefix=f"workflow_run_{run_id}_jobs",
            params={"filter": "latest"}, collection_key="jobs",
        )
    endpoint = f"repos/{repo}/actions/runs/{run_id}/jobs"
    data = gh_json(["api", endpoint, "-X", "GET", "-f", "per_page=100"], repo=repo)
    if not isinstance(data, dict):
        raise GhCommandError("Unexpected payload from actions run jobs API")
    jobs = data.get("jobs") or []
    if not isinstance(jobs, list):
        raise GhCommandError("Expected `jobs` to be a list")
    return jobs


def failed_jobs_from_workflow_runs(repo, runs, head_sha, reader=None):
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
        # Job detail is diagnostic evidence, not a per-poll health signal. A
        # completed failed run needs it; queued/running runs do not.
        if run_status.lower() != "completed" or run_conclusion not in FAILED_RUN_CONCLUSIONS:
            continue
        jobs = get_jobs_for_run(repo, run_id, **({"reader": reader} if reader is not None else {}))
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


def get_authenticated_login(reader=None):
    configured = github_identity.automation_login()
    if configured:
        return configured
    if reader is not None:
        data = reader.get_json("/user", step="authenticated_user")
        if isinstance(data, dict) and data.get("login"):
            return str(data["login"])
        raise GhCommandError("Unable to determine authenticated GitHub login from GitHub REST API")
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


def gh_api_list_paginated(endpoint, repo=None, per_page=100, reader=None):
    if reader is not None:
        return reader.paged_json(endpoint if endpoint.startswith("/") else f"/{endpoint}", step_prefix="review_items", params={"per_page": per_page})
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
        user = item.get("user")
        out.append(
            {
                "kind": "issue_comment",
                "id": str(item.get("id") or ""),
                "author": extract_login(item.get("user")),
                "author_id": user.get("id") if isinstance(user, dict) else None,
                "github_app": item.get("performed_via_github_app"),
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


def launchplane_owner_review(item, pr):
    """Verify a projected decision against Launchplane's saved record and receipt.

    The saved receipt establishes the projection, not permission to merge.
    The complete reason remains Owner feedback; it is never an agent instruction.
    """
    if item.get("kind") != "issue_comment":
        return None
    lines = str(item.get("body") or "").splitlines()
    marker = re.fullmatch(r"<!-- launchplane:product-review:([A-Za-z0-9_.:-]+) -->", lines[0]) if lines else None
    if marker is None:
        return None
    if type(item.get("author_id")) is not int or item["author_id"] < 1:
        raise GhCommandError("Unverified Launchplane Owner feedback publisher")
    prefix = "<!-- launchplane:owner-review "
    if len(lines) < 2 or not lines[1].startswith(prefix) or not lines[1].endswith(" -->"):
        raise GhCommandError("Launchplane Owner feedback metadata is missing")
    try:
        decision = json.loads(lines[1][len(prefix):-4])
    except json.JSONDecodeError as error:
        raise GhCommandError("Launchplane Owner feedback metadata is invalid") from error
    if (
        not isinstance(decision, dict)
        or type(decision.get("schema_version")) is not int
        or decision["schema_version"] != 1
        or decision.get("record_id") != marker[1]
        or str(decision.get("repository") or "").casefold() != pr["repo"].casefold()
        or type(decision.get("pull_request_number")) is not int
        or decision["pull_request_number"] != pr["number"]
        or not isinstance(decision.get("decision"), str)
        or decision["decision"] not in {"accepted", "changes_requested"}
        or not isinstance(decision.get("reason"), str)
        or not re.fullmatch(r"[0-9a-fA-F]{40}", str(decision.get("head_sha") or ""))
        or not str(decision.get("owner_github_id") or "").isdecimal()
        or not isinstance(decision.get("owner_github_login"), str)
        or not decision["owner_github_login"].strip()
        or not isinstance(decision.get("decided_at"), str)
        or not decision["decided_at"].strip()
        or (decision["decision"] == "changes_requested" and not decision["reason"].strip())
    ):
        raise GhCommandError("Launchplane Owner feedback does not identify a complete decision for this PR")
    try:
        reference = urlparse(str(decision.get("review_url") or ""))
        query = parse_qs(reference.query)
        valid_reference = (
            reference.scheme in {"http", "https"}
            and bool(reference.netloc)
            and reference.username is None
            and reference.password is None
            and reference.path == "/ui/owner-review"
            and query.get("repository") == [decision["repository"]]
            and query.get("pull_request") == [str(pr["number"])]
            and query.get("decision_id") == [decision["record_id"]]
        )
    except ValueError:
        valid_reference = False
    if not valid_reference:
        raise GhCommandError("Launchplane Owner feedback decision link does not match this PR")
    saved = read_launchplane_owner_review(pr, decision["record_id"])
    fields = (
        "record_id", "product", "repository", "pull_request_number", "head_sha",
        "preview_url", "decision", "reason", "owner_github_id", "owner_github_login", "decided_at",
    )
    if any(saved.get(key) != decision.get(key) for key in fields) or str(saved.get("feedback_url") or "").casefold() != str(item.get("url") or "").casefold():
        raise GhCommandError("Launchplane Owner feedback differs from the saved decision or delivery receipt")
    return owner_review_metadata(decision, pr)


def owner_review_metadata(decision, pr):
    try:
        decided_at = datetime.fromisoformat(decision["decided_at"])
        if decided_at.tzinfo is None:
            raise ValueError("missing timezone")
    except ValueError as error:
        raise GhCommandError("Launchplane Owner feedback timestamp is invalid") from error
    return {
        **decision,
        "decided_at_epoch": decided_at.timestamp(),
        "matches_current_head": decision["head_sha"].casefold() == pr["head_sha"].casefold(),
    }


def read_launchplane_owner_review(pr, decision_id=""):
    """Read authority using private configured routing, never the comment's URL."""
    try:
        result = subprocess.run(
            [sys.executable, OWNER_REVIEW_HELPER, "--repo", pr["repo"], "--pr", str(pr["number"]), "--decision-id", decision_id],
            capture_output=True, text=True, timeout=20,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        raise GhCommandError("Launchplane Owner review verification is unavailable") from error
    if not result.returncode and isinstance(payload, dict) and payload.get("ok") is True and payload.get("decision") is None and not decision_id:
        return None
    if result.returncode or not isinstance(payload, dict) or payload.get("ok") is not True or not isinstance(payload.get("decision"), dict):
        raise GhCommandError("Launchplane Owner review verification failed; the saved decision could not be read")
    return payload["decision"]


def fetch_new_review_items(pr, state, fresh_state, authenticated_login=None, reader=None):
    del fresh_state  # Existing unseen feedback is surfaced for both fresh and resumed state.
    repo = pr["repo"]
    pr_number = pr["number"]
    endpoints = comment_endpoints(repo, pr_number)

    extra = {"reader": reader} if reader is not None else {}
    issue_payload = gh_api_list_paginated(endpoints["issue_comment"], repo=repo, **extra)
    review_comment_payload = gh_api_list_paginated(endpoints["review_comment"], repo=repo, **extra)
    review_payload = gh_api_list_paginated(endpoints["review"], repo=repo, **extra)

    issue_items = normalize_issue_comments(issue_payload)
    review_comment_items = normalize_review_comments(review_comment_payload)
    review_items = normalize_reviews(review_payload)
    threads = fetch_review_threads(pr, review_comment_items, reader)
    pr["review_threads"] = threads
    by_comment = {
        comment["comment_id"]: {**thread, **comment}
        for thread in threads["threads"] for comment in thread["comments"]
    }
    for item in review_comment_items:
        item["head_sha"] = pr["head_sha"]
        if item["id"] in by_comment:
            item["thread"] = by_comment[item["id"]]
    all_items = issue_items + review_comment_items + review_items

    seen_issue = {str(x) for x in state.get("seen_issue_comment_ids") or []}
    seen_review_comment = {str(x) for x in state.get("seen_review_comment_ids") or []}
    seen_review = {str(x) for x in state.get("seen_review_ids") or []}

    # On a brand-new state file, surface existing review activity instead of
    # silently treating it as seen. This avoids missing already-pending review
    # feedback when monitoring starts after comments were posted.

    new_items = []
    owner_review_items = []
    owner_review_errors = []
    previous_owner_reviews = {item["id"]: item for item in state.get("owner_review_items", [])}
    for item in all_items:
        item_id = item.get("id")
        if not item_id:
            continue
        author = item.get("author") or ""
        if not author:
            continue
        try:
            owner_review = launchplane_owner_review(item, pr)
        except GhCommandError as error:
            owner_review_errors.append({"id": item_id, "url": item.get("url"), "error": str(error)})
            previous = previous_owner_reviews.get(item_id)
            if previous is not None:
                owner_review_items.append({
                    **previous,
                    "verification_status": "unavailable",
                    "owner_review": {
                        **previous["owner_review"],
                        "matches_current_head": previous["owner_review"]["head_sha"].casefold() == pr["head_sha"].casefold(),
                    },
                })
            # Do not mark this comment as seen or treat its unchecked body as
            # Owner feedback. Keep CI and PR-state monitoring available.
            continue
        if owner_review is not None:
            item["source"] = "launchplane_owner_review"
            item["verification_status"] = "verified"
            item["owner_review"] = owner_review
            owner_review_items.append(item)
        elif is_bot_login(author):
            if not is_actionable_review_bot_login(author):
                continue
        elif not is_external_human_review_author(item, authenticated_login):
            continue

        kind = item["kind"]
        if kind == "review_comment" and item.get("thread", {}).get("is_resolved") is True:
            seen_review_comment.add(item_id)
            continue
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

    if owner_review_items or previous_owner_reviews:
        try:
            latest = read_launchplane_owner_review(pr)
            if latest is None:
                raise GhCommandError("Latest Launchplane Owner decision is unavailable")
            if latest["record_id"] not in {item["owner_review"]["record_id"] for item in owner_review_items}:
                owner_review_items.append({
                    "id": latest["record_id"], "kind": "launchplane_decision", "url": latest["feedback_url"],
                    "source": "launchplane_owner_review", "verification_status": "verified",
                    "owner_review": owner_review_metadata(latest, pr),
                })
        except GhCommandError as error:
            owner_review_errors.append({"id": "latest", "url": "", "error": str(error)})
            for previous in previous_owner_reviews.values():
                if previous.get("kind") == "launchplane_decision":
                    owner_review_items.append({**previous, "verification_status": "unavailable",
                        "owner_review": owner_review_metadata(previous["owner_review"], pr)})

    new_items.sort(key=lambda review_item: (review_item.get("created_at") or "", review_item.get("kind") or "", review_item.get("id") or ""))
    state["seen_issue_comment_ids"] = sorted(seen_issue)
    state["seen_review_comment_ids"] = sorted(seen_review_comment)
    state["seen_review_ids"] = sorted(seen_review)
    # Seen means emitted, not acknowledged by an agent. Retain full decisions on
    # every snapshot so a resumed session can recover the prose and its revision.
    state["owner_review_items"] = owner_review_items
    state["owner_review_errors"] = owner_review_errors
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
    if checks_summary.get("evidence_complete") is not True:
        return False
    if checks_summary.get("head_matches") is not True:
        return False
    if checks_summary["failed_count"] > 0 or checks_summary["pending_count"] > 0:
        return False
    if new_review_items:
        return False
    threads = pr.get("review_threads", {})
    if threads.get("status") == "unknown" or any(not thread["is_resolved"] for thread in threads.get("threads", [])):
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
    requirement = pr.get("review_requirement")
    if requirement == "satisfied" and pr.get("review_decision_source") == "graphql":
        return True
    return requirement == "not_applicable" and pr.get("review_decision_source") == "graphql" and pr.get("merge_state_status") == "CLEAN"


def is_review_readiness_unavailable(pr, checks_summary, new_review_items):
    availability = pr.get("metadata_availability")
    return (
        not pr["closed"]
        and not pr["merged"]
        and not pr.get("draft")
        and checks_summary["all_terminal"]
        and checks_summary.get("evidence_complete") is True
        and checks_summary.get("head_matches") is True
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


def recommend_actions(pr, checks_summary, failed_runs, failed_jobs, new_review_items, retries_used, max_retries, owner_review_items=(), owner_review_errors=()):
    actions = []
    if owner_review_errors:
        actions.append("owner_review_verification_unavailable")
    if pr["closed"] or pr["merged"]:
        if new_review_items:
            actions.append("process_review_comment")
        actions.append("stop_pr_closed")
        return unique_actions(actions)

    verified_owner_reviews = [item["owner_review"] for item in owner_review_items if item.get("verification_status", "verified") == "verified"]
    latest_known_owner_review = max(
        verified_owner_reviews,
        key=lambda decision: (decision["decided_at_epoch"], decision["record_id"]), default=None,
    )
    if latest_known_owner_review and not latest_known_owner_review["matches_current_head"] and latest_known_owner_review["decision"] == "changes_requested":
        actions.append("review_owner_feedback_history")
    delivery_pending = any(item.get("kind") == "launchplane_decision" and not item.get("url") for item in owner_review_items)
    if delivery_pending:
        actions.append("owner_feedback_delivery_pending")

    current_owner_reviews = [
        item["owner_review"] for item in owner_review_items
        if item["owner_review"]["matches_current_head"] and item.get("verification_status", "verified") == "verified"
    ]
    latest_owner_review = max(
        current_owner_reviews,
        key=lambda decision: (decision["decided_at_epoch"], decision["record_id"]),
        default=None,
    )
    owner_changes_requested = (
        latest_owner_review is not None
        and latest_owner_review["decision"] == "changes_requested"
    )
    if owner_changes_requested:
        actions.append("address_owner_review_changes")

    if not owner_review_errors and not delivery_pending and not owner_changes_requested and is_pr_ready_to_merge(pr, checks_summary, new_review_items):
        actions.append("ready_to_merge")
        return unique_actions(actions)

    if pr.get("review_requirement") == "pending":
        actions.append("awaiting_review")
    elif pr.get("review_requirement") == "changes_requested":
        actions.append("address_review_changes")

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
    threads = pr.get("review_threads", {})
    if threads.get("status") == "unknown":
        actions.append("review_thread_resolution_unavailable")
    elif any(not thread["is_resolved"] for thread in threads.get("threads", [])):
        actions.append("resolve_review_threads")

    if checks_summary.get("evidence_complete") is not True:
        actions.append("check_evidence_incomplete")

    if has_failed_pr_checks:
        if checks_summary["all_terminal"] and retries_used >= max_retries:
            actions.append("stop_exhausted_retries")
        else:
            actions.append("diagnose_ci_failure")
            if checks_summary["all_terminal"] and failed_runs and retries_used < max_retries:
                actions.append("retry_failed_checks")

    if (
        not pr.get("draft")
        and pr.get("merge_state_status") == "BEHIND"
        and pr.get("metadata_availability", {}).get("merge_state_status") is True
    ):
        actions.append("update_behind_branch")

    if not actions:
        actions.append("idle")
    return unique_actions(actions)


def collect_snapshot(args):
    pr = resolve_pr(args.pr, repo_override=args.repo)
    pr_diagnostic = pr.pop("_read_diagnostic", None)
    state_path = Path(args.state_file) if args.state_file else default_state_file_for(pr)
    with state_lock(state_path):
        if not args.state_file and not state_path.exists():
            legacy_path = legacy_state_file_for(pr)
            with state_lock(legacy_path):
                if legacy_path.exists():
                    legacy_state, _ = load_state(legacy_path)
                    save_state(state_path, legacy_state)
        return collect_locked_snapshot(args, pr, pr_diagnostic, state_path)


def collect_locked_snapshot(args, pr, pr_diagnostic, state_path):
    state, fresh_state = load_state(state_path)

    if not state.get("started_at"):
        state["started_at"] = int(time.time())

    reader = watcher_reader()
    authenticated_login = get_authenticated_login(reader)
    new_review_items = fetch_new_review_items(
        pr,
        state,
        fresh_state=fresh_state,
        authenticated_login=authenticated_login,
        reader=reader,
    )
    # Surface review feedback before drilling into CI and mergeability details.
    # That keeps the babysitter responsive to new comments even when other
    # actions are also available.
    # After resolving `--pr auto`, give the REST-first checks helper the
    # concrete PR number so both reads stay pinned to one target.
    checks = github_read.pull_request_checks(
        reader, pr["repo"], pr["number"], head_sha=pr["head_sha"]
    )
    checks_diagnostic = reader.diagnostics()
    checks_summary = summarize_checks(checks, expected_head_sha=pr["head_sha"])
    workflow_runs = get_workflow_runs_for_sha(pr["repo"], pr["head_sha"], reader=reader)
    checks_summary = apply_unfinished_workflow_runs(checks_summary, workflow_runs, pr["head_sha"])
    failed_runs = failed_runs_from_workflow_runs(workflow_runs, pr["head_sha"])
    failed_jobs = failed_jobs_from_workflow_runs(pr["repo"], workflow_runs, pr["head_sha"], reader=reader)
    pending_reruns = reconcile_pending_reruns(state, pr["head_sha"], workflow_runs)
    present_ids = {str(run.get("id")) for run in workflow_runs if run.get("head_sha") == pr["head_sha"]}
    missing_confirmed_ids = [run_id for run_id, intent in pending_reruns.items()
                             if intent["outcome"] == "confirmed" and run_id not in present_ids]
    rejected = reconcile_rejected_reruns(state, pr["head_sha"], workflow_runs)
    retries_used = current_retry_count(state, pr["head_sha"])
    if (not pr["closed"] and not pr["merged"] and not pending_reruns
            and checks_summary.get("evidence_complete") is True and checks_summary["all_terminal"]
            and retries_used < args.max_flaky_retries):
        ordinary_retry_ids = {run["run_id"] for run in retryable_failed_runs(failed_runs, failed_jobs)}
        for run in failed_runs:
            if run["run_id"] not in ordinary_retry_ids and runner_acquisition_retry(pr, run, reader):
                run["retry_mode"] = "runner_acquisition"

    review_diagnostic = None
    if is_review_readiness_unavailable(pr, checks_summary, new_review_items):
        review, _ = query_review_readiness(pr, reader)
        apply_review_readiness(pr, review)
        review_diagnostic = review_readiness_diagnostic(reader, review)

    actions = recommend_actions(
        pr,
        checks_summary,
        failed_runs,
        failed_jobs,
        new_review_items,
        retries_used,
        args.max_flaky_retries,
        owner_review_items=state.get("owner_review_items", []),
        owner_review_errors=state.get("owner_review_errors", []),
    )

    if pending_reruns and not pr["closed"] and not pr["merged"]:
        actions = [action for action in actions if action not in {
            "retry_failed_checks", "ready_to_merge", "stop_exhausted_retries",
        }]
        actions.append("check_rerun_outcome")
        if any(item["outcome"] != "confirmed" for item in pending_reruns.values()):
            actions.append("stop_unknown_rerun")
        if missing_confirmed_ids:
            actions.append("stop_missing_rerun")
    elif "retry_failed_checks" in actions and not retryable_failed_runs(failed_runs, failed_jobs):
        actions.remove("retry_failed_checks")
    elif "retry_failed_checks" in actions and all(
        rejected.get(str(run["run_id"]), {}).get("run_attempt") == run["run_attempt"]
        for run in retryable_failed_runs(failed_runs, failed_jobs)
    ):
        actions.remove("retry_failed_checks")
        actions.append("stop_nonretryable_rerun")

    state["pr"] = {"repo": pr["repo"], "number": pr["number"]}
    state["last_seen_head_sha"] = pr["head_sha"]
    state["last_snapshot_at"] = int(time.time())
    save_state(state_path, state)

    snapshot: dict[str, Any] = {
        "pr": pr,
        "checks": checks_summary,
        "failed_runs": failed_runs,
        "failed_jobs": failed_jobs,
        "new_review_items": new_review_items,
        "owner_review_items": state.get("owner_review_items", []),
        "owner_review_errors": state.get("owner_review_errors", []),
        "actions": actions,
        "retry_state": {
            "current_sha_retries_used": retries_used,
            "max_flaky_retries": args.max_flaky_retries,
            "pending_run_ids": list(pending_reruns),
            "missing_confirmed_run_ids": missing_confirmed_ids,
            "rejected_run_ids": list(rejected),
        },
        "minimum_poll_seconds": max((github_read.poll_interval(result.headers) for result in reader.results), default=0.0),
        "read_diagnostics": {
            "pr": pr_diagnostic,
            "checks": checks_diagnostic,
            "review": review_diagnostic,
        },
    }
    return snapshot, state_path


def retryable_failed_runs(failed_runs, failed_jobs):
    failed_job_runs = {
        job.get("run_id") for job in failed_jobs
        if job.get("status") == "completed"
        and job.get("conclusion") in {"failure", "timed_out"}
    }
    return [run for run in failed_runs if run.get("run_id") in failed_job_runs
            or run.get("retry_mode") == "runner_acquisition"]


def runner_acquisition_retry(pr, run, reader, *, raise_read_errors=False):
    """Admit a full retry only when no job executed and a check is required."""
    run_id, attempt = run.get("run_id"), run.get("run_attempt")
    if (run.get("status") != "completed" or run.get("conclusion") != "failure"
            or type(attempt) is not int or attempt <= 0 or type(run_id) is not int):
        return False
    try:
        jobs = reader.paged_json(
            f"/repos/{pr['repo']}/actions/runs/{run_id}/attempts/{attempt}/jobs",
            step_prefix="acquisition_jobs", collection_key="jobs",
        )
        if not jobs:
            return False
        required = False
        for job in jobs:
            if (job.get("status") != "completed" or job.get("conclusion") != "cancelled"
                    or job.get("steps") != [] or job.get("head_sha") != pr["head_sha"]
                    or job.get("run_id") != run_id or job.get("run_attempt") != attempt
                    or job.get("runner_id") != 0):
                return False
            path = urlparse(str(job.get("check_run_url") or ""))
            prefix = f"/repos/{pr['repo']}/check-runs/"
            if (path.scheme != "https" or path.netloc != "api.github.com"
                    or not path.path.casefold().startswith(prefix.casefold()) or not path.path[len(prefix):].isdigit()
                    or path.query or path.fragment):
                return False
            check = reader.get_json(path.path, step="acquisition_check")
            if (check.get("head_sha") != pr["head_sha"] or check.get("conclusion") != "cancelled"
                    or check.get("id") != int(path.path[len(prefix):])):
                return False
            annotations = reader.paged_json(
                path.path + "/annotations", step_prefix="acquisition_annotations",
            )
            message = "The job was not acquired by Runner of type hosted even after multiple attempts"
            if not any(a.get("annotation_level") == "failure"
                       and str(a.get("message") or "").strip().rstrip(".") == message
                       for a in annotations):
                return False
            result = reader.graphql_json(
                "query($id: ID!, $number: Int!) { node(id: $id) { ... on CheckRun { "
                "databaseId isRequired(pullRequestNumber: $number) "
                "checkSuite { repository { nameWithOwner } } } } }",
                {"id": check.get("node_id"), "number": pr["number"]},
                step="acquisition_required_check",
                operation="github.pr.runner_acquisition_evidence",
                retry_policy=github_api.RetryPolicy(max_wait_seconds=2.0, max_attempts=1),
                deadline_at=time.time() + 2.0,
            )
            body = result.body if result.ok else None
            data = body.get("data") if isinstance(body, dict) else None
            node = data.get("node") if isinstance(data, dict) else None
            suite = node.get("checkSuite") if isinstance(node, dict) else None
            repository = suite.get("repository") if isinstance(suite, dict) else None
            if (not isinstance(node, dict) or body.get("errors")
                    or node.get("databaseId") != check.get("id")
                    or not isinstance(repository, dict)
                    or str(repository.get("nameWithOwner") or "").casefold() != pr["repo"].casefold()
                    or type(node.get("isRequired")) is not bool):
                return False
            required = required or node["isRequired"]
        fresh_run = reader.get_json(f"/repos/{pr['repo']}/actions/runs/{run_id}", step="acquisition_run_readback")
        if not required:
            required = required_codeql_workflow(pr, fresh_run, reader)
        return (required and fresh_run.get("id") == run_id
                and fresh_run.get("head_sha") == pr["head_sha"]
                and fresh_run.get("run_attempt") == attempt
                and fresh_run.get("status") == "completed" and fresh_run.get("conclusion") == "failure"
                and not any(r.get("component") == "actor" for r in reader.degraded_reasons))
    except github_read.GitHubReadError:
        if raise_read_errors:
            raise
        return False
    except (github_read.GitHubReadShapeError, GhCommandError):
        return False


def required_codeql_workflow(pr, run, reader):
    """Code-scanning rules require a tool rather than its Actions job check."""
    path = run.get("path")
    if (not isinstance(path, str) or not path.startswith(".github/workflows/")
            or not path.endswith((".yml", ".yaml")) or ".." in path.split("/")):
        return False
    rules = reader.paged_json(
        f"/repos/{pr['repo']}/rules/branches/{quote(pr['base_branch'], safe='')}",
        step_prefix="acquisition_scan_rules",
    )
    if not any(rule.get("type") == "code_scanning" and any(
            tool.get("tool") == "CodeQL"
            for tool in rule.get("parameters", {}).get("code_scanning_tools", [])
    ) for rule in rules):
        return False
    content = reader.get_json(
        f"/repos/{pr['repo']}/contents/{quote(path, safe='/')}?ref={quote(pr['head_sha'], safe='')}",
        step="acquisition_workflow_source",
    )
    if content.get("encoding") != "base64" or content.get("path") != path:
        return False
    try:
        workflow = yaml.safe_load(base64.b64decode(content["content"]).decode("utf-8"))
    except (KeyError, ValueError, yaml.YAMLError):
        return False
    if not isinstance(workflow, dict) or not isinstance(workflow.get("jobs"), dict):
        return False
    return any(
        isinstance(job, dict) and isinstance(job.get("steps"), list) and any(
            isinstance(step, dict) and isinstance(step.get("uses"), str)
            and step["uses"].startswith("github/codeql-action/analyze@")
            for step in job["steps"]
        ) for job in workflow["jobs"].values()
    )


def reconcile_pending_reruns(state, head_sha, workflow_runs):
    pending = state.setdefault("pending_reruns_by_sha", {}).setdefault(head_sha, {})
    for run in workflow_runs:
        run_id = str(run.get("id"))
        previous_attempt = pending.get(run_id, {}).get("run_attempt")
        current_attempt = run.get("run_attempt")
        if (run.get("head_sha") == head_sha and isinstance(previous_attempt, int)
                and isinstance(current_attempt, int) and current_attempt > previous_attempt):
            del pending[run_id]
    return pending


def reconcile_rejected_reruns(state, head_sha, workflow_runs):
    rejected = state.setdefault("rejected_reruns_by_sha", {}).setdefault(head_sha, {})
    for run in workflow_runs:
        run_id = str(run.get("id"))
        prior = rejected.get(run_id, {}).get("run_attempt")
        attempt = run.get("run_attempt")
        if (run.get("head_sha") == head_sha and isinstance(prior, int)
                and isinstance(attempt, int) and attempt > prior):
            del rejected[run_id]
    return rejected


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
        "skipped_run_ids": [],
        "reason": None,
    }

    if pr["closed"] or pr["merged"]:
        result["reason"] = "pr_closed"
        return result
    if snapshot["retry_state"].get("pending_run_ids"):
        result["reason"] = "rerun_outcome_pending"
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

    eligible_runs = retryable_failed_runs(failed_runs, snapshot.get("failed_jobs", []))
    result["skipped_run_ids"] = [
        run.get("run_id") for run in failed_runs if run not in eligible_runs
    ]
    try:
        with state_lock(state_path):
            return submit_locked_reruns(snapshot, state_path, result, eligible_runs)
    except OSError as err:
        result["reason"] = "state_save_error"
        result["error"] = github_api.redact_string(str(err))
        try:
            result["retries_used"] = current_retry_count(load_state(state_path)[0], pr["head_sha"])
        except (OSError, RuntimeError):
            result["retry_budget_unavailable"] = True
        return result


def save_unsent_state(state_path, state, result):
    try:
        save_state(state_path, state)
    except OSError as err:
        result["unsent_state_restored"] = False
        result["recovery_error"] = github_api.redact_string(str(err))
        raise
    result["unsent_state_restored"] = True


def submit_locked_reruns(snapshot, state_path, result, eligible_runs):
    pr = snapshot["pr"]
    state, _ = load_state(state_path)
    retries_used = current_retry_count(state, pr["head_sha"])
    if retries_used >= snapshot["retry_state"]["max_flaky_retries"]:
        result["reason"] = "retry_budget_exhausted"
        return result
    pending = state.setdefault("pending_reruns_by_sha", {}).setdefault(pr["head_sha"], {})
    if pending:
        result["reason"] = "rerun_outcome_pending"
        return result
    recovery_runs = [run for run in eligible_runs if run.get("retry_mode") == "runner_acquisition"]
    submission_reader = watcher_reader()
    if recovery_runs:
        # Pre-write acquisition evidence must reach GitHub, even inside the
        # polling cache's short coalescing window.
        submission_reader.cache_enabled = False
    current_runs = {
        run.get("id"): run for run in get_workflow_runs_for_sha(pr["repo"], pr["head_sha"], reader=submission_reader)
    } if eligible_runs else {}
    rejected_runs = reconcile_rejected_reruns(state, pr["head_sha"], list(current_runs.values()))
    cycle_charged = False
    for run in eligible_runs:
        run_id = run.get("run_id")
        current_run = current_runs.get(run_id, {})
        attempt = run.get("run_attempt")
        if rejected_runs.get(str(run_id), {}).get("run_attempt") == attempt:
            result["skipped_run_ids"].append(run_id)
            continue
        if (not isinstance(attempt, int) or attempt <= 0
                or current_run.get("head_sha") != pr["head_sha"]
                or current_run.get("run_attempt") != attempt
                or current_run.get("status") != "completed"):
            result["skipped_run_ids"].append(run_id)
            continue
        recovery = run.get("retry_mode") == "runner_acquisition"
        if recovery:
            try:
                if not runner_acquisition_retry(pr, {
                    **run, "conclusion": current_run.get("conclusion"),
                }, submission_reader, raise_read_errors=True):
                    result["skipped_run_ids"].append(run_id)
                    continue
                fresh_pr = submission_reader.get_json(
                    f"/repos/{pr['repo']}/pulls/{pr['number']}", step="acquisition_pr_readback",
                )
            except github_read.GitHubReadError as err:
                result["reason"] = "rerun_read_error"
                result["error"] = github_api.redact_string(str(err))
                result["read_error"] = github_api.redact_body(err.result.as_dict())
                break
            if (not isinstance(fresh_pr, dict) or fresh_pr.get("state") != "open"
                    or fresh_pr.get("merged") is not False
                    or (fresh_pr.get("head") or {}).get("sha") != pr["head_sha"]
                    or (fresh_pr.get("base") or {}).get("ref") != pr["base_branch"]):
                result["reason"] = "pr_changed"
                break
        if not cycle_charged:
            set_retry_count(state, pr["head_sha"], retries_used + 1)
            cycle_charged = True
        # Persist intent before submitting a write. A crash or ambiguous error
        # must not let the next invocation replay the same run attempt.
        pending[str(run_id)] = {"run_attempt": run.get("run_attempt"), "outcome": "submitting"}
        if recovery:
            pending[str(run_id)]["retry_mode"] = "runner_acquisition"
        try:
            save_state(state_path, state)
        except OSError:
            # No command has launched. Restore only this unsent intent, keeping
            # earlier confirmed progress and its cycle charge intact.
            result.setdefault("not_sent_run_ids", []).append(run_id)
            del pending[str(run_id)]
            if not result["rerun_run_ids"]:
                set_retry_count(state, pr["head_sha"], retries_used)
            try:
                save_unsent_state(state_path, state, result)
            except OSError:
                pass
            raise
        result["rerun_attempted"] = True
        try:
            gh_text(["run", "rerun", str(run_id)] + ([] if recovery else ["--failed"]), repo=pr["repo"])
        except GhCommandError as err:
            detail = github_api.redact_string(str(err))
            not_sent = isinstance(err, GhCommandNotSent) or isinstance(err.__cause__, FileNotFoundError)
            # gh rewrites HTTP 403 into this message, dropping the status.
            rejected = bool(re.search(r"HTTP (?:400|401|403|404|410|422|429)\b", detail)) or (
                f"run {run_id} cannot be rerun;" in detail
                or "failed to get run:" in detail
                or not_sent
            )
            if rejected:
                if not_sent:
                    result.setdefault("not_sent_run_ids", []).append(run_id)
                del pending[str(run_id)]
                if not result["rerun_run_ids"]:
                    set_retry_count(state, pr["head_sha"], retries_used)
                    cycle_charged = False
                nonretryable = "cannot be retried" in detail.lower()
                if nonretryable:
                    rejected_runs[str(run_id)] = {"run_attempt": attempt, "reason": detail}
                if not_sent:
                    save_unsent_state(state_path, state, result)
                else:
                    save_state(state_path, state)
                if not nonretryable:
                    result["reason"] = "rerun_rejected"
                    result["error"] = detail
                    break
                result["skipped_run_ids"].append(run_id)
                continue
            result["reason"] = "rerun_outcome_unknown"
            result["error"] = detail
            result["unknown_run_id"] = run_id
            break
        pending[str(run_id)]["outcome"] = "confirmed"
        result["rerun_run_ids"].append(run_id)
        result["rerun_count"] = len(result["rerun_run_ids"])
        save_state(state_path, state)

    if result["reason"] not in {"rerun_outcome_unknown", "rerun_rejected", "pr_changed", "rerun_read_error"}:
        if result["rerun_run_ids"]:
            result["reason"] = "rerun_triggered"
        else:
            result["reason"] = "no_rerunnable_failed_jobs"
    state["last_snapshot_at"] = int(time.time())
    save_state(state_path, state)
    result["retries_used"] = current_retry_count(state, pr["head_sha"])

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
    last_change_key = None
    unchanged_polls = 0
    while True:
        try:
            snapshot, state_path = collect_snapshot(args)
        except StateLockTimeout as err:
            print_event("state_busy", {
                "state_file": str(err.path), "next_poll_seconds": args.poll_seconds,
            })
            time.sleep(args.poll_seconds)
            continue
        current_change_key = snapshot_change_key(snapshot)
        changed = current_change_key != last_change_key
        unchanged_polls = 0 if changed else min(unchanged_polls + 1, 10)
        green = is_ci_green(snapshot)
        pr = snapshot.get("pr") or {}
        pr_open = not bool(pr.get("closed")) and not bool(pr.get("merged"))

        if not pr_open or changed or last_change_key is None:
            poll_seconds = args.poll_seconds
        elif not green:
            poll_seconds = min(
                args.poll_seconds * 2 ** unchanged_polls,
                max(args.poll_seconds, getattr(args, "green_poll_seconds", 300)),
            )
        else:
            poll_seconds = getattr(args, "green_poll_seconds", 300)

        poll_seconds = github_read.poll_delay(poll_seconds, snapshot.get("minimum_poll_seconds", 0.0), repository=pr.get("repo"))
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
            or "stop_unknown_rerun" in actions
            or "stop_missing_rerun" in actions
            or "stop_nonretryable_rerun" in actions
        ):
            print_event("stop", {"actions": snapshot.get("actions"), "pr": snapshot.get("pr")})
            return 0

        last_change_key = current_change_key
        time.sleep(poll_seconds)


def main():
    args = parse_args()
    try:
        if args.retry_failed_now:
            result = retry_failed_now(args)
            print_json(result)
            return 1 if result.get("error") else 0
        if args.watch:
            return run_watch(args)
        snapshot, state_path = collect_snapshot(args)
        snapshot["state_file"] = str(state_path)
        print_json(snapshot)
        return 0
    except github_read.GitHubReadError as err:
        print_event("read_error", err.result.as_dict())
        return 1
    except PrHelperReadError as err:
        print_event("read_error", err.payload)
        return 1
    except (GhCommandError, RuntimeError, ValueError, OSError) as err:
        sys.stderr.write(f"gh_pr_watch.py error: {err}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("gh_pr_watch.py interrupted\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
