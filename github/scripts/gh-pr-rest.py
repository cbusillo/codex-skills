#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""REST-first PR helper for GitHub operations that should not burn GraphQL."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any, Optional


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
GH = os.environ.get("GH_PR_REST_GH") or str(SCRIPT_DIR / "gh-with-env-token")
API_VERSION_ARGS = ["-H", "X-GitHub-Api-Version: 2022-11-28"]


class HelperError(Exception):
    pass


def main() -> int:
    args = parse_args()
    try:
        payload = args.func(args)
    except HelperError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use GitHub REST endpoints for PR reads, checks, merge, and rate limits.",
    )
    parser.add_argument("--repo", help="Repository in OWNER/REPO form.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("view", help="Show PR metadata via REST.")
    p.add_argument("pr", nargs="?", help="PR number or URL. Defaults to current branch PR.")
    p.set_defaults(func=cmd_view)

    p = sub.add_parser("checks", help="Show check runs and commit statuses via REST.")
    p.add_argument("pr", nargs="?", help="PR number or URL. Defaults to current branch PR.")
    p.set_defaults(func=cmd_checks)

    p = sub.add_parser("merge", help="Merge a PR via the REST merge endpoint.")
    p.add_argument("pr", help="PR number or URL.")
    p.add_argument("--method", choices=("merge", "squash", "rebase"), default="merge")
    p.add_argument("--commit-title")
    p.add_argument("--commit-message")
    p.add_argument("--delete-branch", action="store_true")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("rate-limit", help="Show REST and GraphQL rate-limit buckets.")
    p.set_defaults(func=cmd_rate_limit)

    return parser.parse_args()


def cmd_view(args: argparse.Namespace) -> dict[str, Any]:
    repo, number = resolve_pr(args.repo, args.pr)
    pr = rest_json("GET", f"/repos/{repo}/pulls/{number}")
    return {"ok": True, "repo": repo, "pr": normalize_pr(pr)}


def cmd_checks(args: argparse.Namespace) -> dict[str, Any]:
    repo, number = resolve_pr(args.repo, args.pr)
    pr = rest_json("GET", f"/repos/{repo}/pulls/{number}")
    sha = pr["head"]["sha"]
    check_runs = paged_rest_json("GET", f"/repos/{repo}/commits/{sha}/check-runs")
    statuses = paged_rest_json("GET", f"/repos/{repo}/commits/{sha}/statuses")
    combined = rest_json("GET", f"/repos/{repo}/commits/{sha}/status")
    checks = [normalize_check_run(item) for item in check_runs]
    status_checks = [normalize_status(item) for item in statuses]
    failing = [item for item in checks if item.get("conclusion") in FAILURE_CONCLUSIONS]
    pending = [item for item in checks if item.get("status") != "completed"]
    failed_statuses = [item for item in status_checks if item.get("state") in {"failure", "error"}]
    pending_statuses = [item for item in status_checks if item.get("state") == "pending"]
    return {
        "ok": True,
        "repo": repo,
        "pr": normalize_pr(pr),
        "headSha": sha,
        "summary": {
            "checkRunCount": len(checks),
            "statusCount": len(status_checks),
            "failingCount": len(failing) + len(failed_statuses),
            "pendingCount": len(pending) + len(pending_statuses),
            "combinedState": combined.get("state") if status_checks else None,
        },
        "checkRuns": checks,
        "statuses": status_checks,
    }


def cmd_merge(args: argparse.Namespace) -> dict[str, Any]:
    repo, number = resolve_pr(args.repo, args.pr)
    pr = rest_json("GET", f"/repos/{repo}/pulls/{number}")
    payload: dict[str, Any] = {"merge_method": args.method, "sha": pr["head"]["sha"]}
    if args.commit_title:
        payload["commit_title"] = args.commit_title
    if args.commit_message:
        payload["commit_message"] = args.commit_message
    result = rest_json("PUT", f"/repos/{repo}/pulls/{number}/merge", payload)
    deleted = None
    if args.delete_branch and result.get("merged"):
        head_repo = pr.get("head", {}).get("repo") or {}
        head_full_name = head_repo.get("full_name")
        head_ref = pr.get("head", {}).get("ref")
        base_ref = pr.get("base", {}).get("ref")
        if head_full_name == repo and head_ref and head_ref != base_ref:
            deleted = delete_ref(repo, f"heads/{head_ref}")
    return {
        "ok": bool(result.get("merged")),
        "repo": repo,
        "pr": normalize_pr(pr),
        "merge": result,
        "deletedBranch": deleted,
    }


def cmd_rate_limit(_args: argparse.Namespace) -> dict[str, Any]:
    data = rest_json("GET", "/rate_limit")
    resources = data.get("resources") or {}
    return {
        "ok": True,
        "core": resources.get("core"),
        "graphql": resources.get("graphql"),
        "search": resources.get("search"),
        "raw": data,
    }


FAILURE_CONCLUSIONS = {"failure", "startup_failure", "timed_out", "cancelled", "action_required"}


def normalize_pr(pr: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": pr.get("number"),
        "title": pr.get("title"),
        "state": pr.get("state"),
        "draft": pr.get("draft"),
        "merged": pr.get("merged"),
        "mergeable": pr.get("mergeable"),
        "mergeable_state": pr.get("mergeable_state"),
        "url": pr.get("html_url"),
        "baseRefName": (pr.get("base") or {}).get("ref"),
        "headRefName": (pr.get("head") or {}).get("ref"),
        "headRefOid": (pr.get("head") or {}).get("sha"),
        "headRepository": ((pr.get("head") or {}).get("repo") or {}).get("full_name"),
        "baseRepository": ((pr.get("base") or {}).get("repo") or {}).get("full_name"),
    }


def normalize_check_run(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": item.get("name"),
        "status": item.get("status"),
        "conclusion": item.get("conclusion"),
        "detailsUrl": item.get("details_url") or item.get("html_url"),
        "startedAt": item.get("started_at"),
        "completedAt": item.get("completed_at"),
        "workflowName": ((item.get("app") or {}).get("name")),
    }


def normalize_status(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "context": item.get("context"),
        "state": item.get("state"),
        "description": item.get("description"),
        "targetUrl": item.get("target_url"),
        "createdAt": item.get("created_at"),
        "updatedAt": item.get("updated_at"),
    }


def resolve_repo(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    remote = run_git(["remote", "get-url", "origin"])
    match = re.search(r"github\.com[:/]([^/]+)/([^/.]+)(?:\.git)?$", remote.strip())
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    data = gh_json(["repo", "view", "--json", "nameWithOwner"])
    value = data.get("nameWithOwner") if isinstance(data, dict) else None
    if value:
        return str(value)
    raise HelperError("Could not resolve repository; pass --repo OWNER/REPO")


def resolve_pr(explicit_repo: Optional[str], value: Optional[str]) -> tuple[str, int]:
    url_ref = parse_pr_url(value)
    if url_ref:
        return url_ref
    repo = resolve_repo(explicit_repo)
    return repo, resolve_pr_number(repo, value)


def parse_pr_url(value: Optional[str]) -> tuple[str, int] | None:
    if not value:
        return None
    match = re.search(r"^(?:https?://|git@)?[^/:]+[:/]([^/]+)/([^/]+)/(?:pull|pulls)/(\d+)(?:[/?#].*)?$", value)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}", int(match.group(3))


def resolve_pr_number(repo: str, value: Optional[str]) -> int:
    if value:
        if value.isdigit():
            return int(value)
        raise HelperError(f"Could not parse PR number: {value}")
    branch = run_git(["branch", "--show-current"]).strip()
    if not branch:
        raise HelperError("Current branch is detached; pass a PR number")
    pulls = rest_json("GET", f"/repos/{repo}/pulls", params={"head": f"{repo.split('/')[0]}:{branch}", "state": "open"})
    if isinstance(pulls, list) and pulls:
        return int(pulls[0]["number"])
    raise HelperError(f"No open PR found for current branch {branch}; pass a PR number")


def rest_json(
    method: str,
    path: str,
    payload: Optional[dict] = None,
    *,
    params: Optional[dict] = None,
) -> Any:
    args = ["api", "--method", method, *API_VERSION_ARGS]
    if params:
        for key, value in params.items():
            args.extend(["-f", f"{key}={value}"])
    if payload is not None:
        for key, value in payload.items():
            args.extend(["-f", f"{key}={value}"])
    args.append(path)
    return gh_json(args)


def paged_rest_json(method: str, path: str) -> list[dict[str, Any]]:
    separator = "&" if "?" in path else "?"
    data = gh_json([
        "api",
        "--method",
        method,
        *API_VERSION_ARGS,
        "--paginate",
        "--slurp",
        f"{path}{separator}per_page=100",
    ])
    pages = data if isinstance(data, list) else [data]
    items: list[dict[str, Any]] = []
    for page in pages:
        if isinstance(page, list):
            items.extend(item for item in page if isinstance(item, dict))
            continue
        if isinstance(page, dict):
            for key in ("check_runs", "statuses"):
                value = page.get(key)
                if isinstance(value, list):
                    items.extend(item for item in value if isinstance(item, dict))
    return items


def delete_ref(repo: str, ref: str) -> dict[str, Any]:
    proc = run_gh(["api", "--method", "DELETE", *API_VERSION_ARGS, f"/repos/{repo}/git/refs/{ref}"])
    return {"ref": ref, "deleted": proc.returncode == 0, "stderr": proc.stderr.strip()}


def gh_json(args: list[str]) -> Any:
    proc = run_gh(args)
    if proc.returncode != 0:
        raise HelperError((proc.stderr or proc.stdout or "gh api failed").strip())
    if not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise HelperError(f"Expected JSON from gh, got: {proc.stdout[:300]}") from exc


def run_gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([GH, *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def run_git(args: list[str]) -> str:
    proc = subprocess.run(["git", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise HelperError(proc.stderr.strip() or "git command failed")
    return proc.stdout


if __name__ == "__main__":
    raise SystemExit(main())
