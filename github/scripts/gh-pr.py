#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Intent-oriented PR helper that hides GitHub transport details from agents."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.parse
from typing import Any, Optional, Tuple


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
GH = os.environ.get("GH_PR_GH") or str(SCRIPT_DIR / "gh-with-env-token")
API_VERSION_ARGS = ["-H", "X-GitHub-Api-Version: 2022-11-28"]


class HelperError(Exception):
    pass


class PrHelperError(HelperError):
    def __init__(self, message: str, **payload: Any):
        super().__init__(message)
        self.payload = payload


def main() -> int:
    args = parse_args()
    try:
        payload = args.func(args)
    except PrHelperError as exc:
        error_payload = {"ok": False, "error": str(exc), **exc.payload}
        print(json.dumps(error_payload, sort_keys=True), file=sys.stderr)
        return 1
    except HelperError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Handle GitHub PR reads, checks, merge, and rate limits with transport-aware defaults.",
    )
    parser.add_argument("--repo", help="Repository in OWNER/REPO form.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("view", help="Show PR metadata via REST.")
    p.add_argument("pr", nargs="?", help="PR number or URL. Defaults to current branch PR.")
    p.set_defaults(func=cmd_view)

    p = sub.add_parser("list", help="List pull requests via REST.")
    p.add_argument("--state", choices=("open", "closed", "all"), default="open")
    p.add_argument("--limit", type=int, default=20, help="Maximum PRs to return.")
    p.set_defaults(func=cmd_list)

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

    p = sub.add_parser("supersede", help="Mark one PR as superseded by another PR.")
    p.add_argument("pr", help="Superseded PR number or URL.")
    p.add_argument("--by", required=True, help="Canonical replacement PR number or URL.")
    p.add_argument("--reason", help="Optional reason to include in the superseded PR comment.")
    p.add_argument("--keep-open", action="store_true", help="Comment and neutralize closing keywords without closing the PR.")
    p.add_argument("--no-neutralize", action="store_true", help="Do not rewrite closing keywords in the superseded PR body.")
    p.add_argument("--dry-run", action="store_true", help="Return the planned operations without changing GitHub state.")
    p.set_defaults(func=cmd_supersede)

    p = sub.add_parser("rate-limit", help="Show REST and GraphQL rate-limit buckets.")
    p.set_defaults(func=cmd_rate_limit)

    return parser.parse_args()


def cmd_view(args: argparse.Namespace) -> dict[str, Any]:
    repo, number = resolve_pr(args.repo, args.pr)
    pr = rest_json("GET", f"/repos/{repo}/pulls/{number}")
    return {"ok": True, "repo": repo, "pr": normalize_pr(pr)}


def cmd_list(args: argparse.Namespace) -> dict[str, Any]:
    repo = resolve_repo(args.repo)
    limit = max(args.limit, 0)
    pulls = limited_paged_rest_json("GET", f"/repos/{repo}/pulls?state={args.state}", limit)
    return {"ok": True, "repo": repo, "pullRequests": [normalize_pr(item) for item in pulls]}


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
    combined_state = combined.get("state")
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
            "combinedState": combined_state if status_checks else None,
            "combinedStateRaw": combined_state,
            "legacyStatusesPresent": bool(status_checks),
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
    merge_path = f"/repos/{repo}/pulls/{number}/merge"
    try:
        result = rest_json("PUT", merge_path, payload)
    except HelperError as exc:
        raise PrHelperError(
            "PR merge failed",
            detail=str(exc),
            operation="merge",
            repo=repo,
            pr=number,
            endpoint=merge_path,
            method=args.method,
            headSha=pr["head"]["sha"],
            hint=merge_failure_hint(str(exc)),
        ) from exc
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


def cmd_supersede(args: argparse.Namespace) -> dict[str, Any]:
    repo, number = resolve_pr(args.repo, args.pr)
    winner_repo, winner_number = resolve_pr(args.repo, args.by)
    if repo == winner_repo and number == winner_number:
        raise PrHelperError("A PR cannot supersede itself", repo=repo, pr=number)

    pr = rest_json("GET", f"/repos/{repo}/pulls/{number}")
    winner = rest_json("GET", f"/repos/{winner_repo}/pulls/{winner_number}")
    original_body = pr.get("body") or ""
    updated_body = original_body
    replacements: list[dict[str, str]] = []
    if not args.no_neutralize:
        updated_body, replacements = neutralize_issue_closing_keywords(original_body)

    winner_ref = pr_reference(repo, winner_repo, winner_number, winner.get("html_url"))
    comment_body = superseded_comment_body(
        winner_ref=winner_ref,
        reason=args.reason,
        body_neutralized=bool(replacements),
        keep_open=args.keep_open,
    )
    planned_close = not args.keep_open

    if args.dry_run:
        return {
            "ok": True,
            "dryRun": True,
            "repo": repo,
            "pr": normalize_pr(pr),
            "supersededBy": {"repo": winner_repo, "number": winner_number, "url": winner.get("html_url")},
            "planned": {
                "updateBody": bool(replacements),
                "commentBody": comment_body,
                "close": planned_close,
            },
            "neutralizedClosingReferences": replacements,
        }

    body_update = None
    if replacements:
        body_update = rest_json("PATCH", f"/repos/{repo}/pulls/{number}", {"body": updated_body})

    comment = rest_json("POST", f"/repos/{repo}/issues/{number}/comments", {"body": comment_body})
    close_result = None
    if planned_close and pr.get("state") != "closed":
        close_result = rest_json("PATCH", f"/repos/{repo}/pulls/{number}", {"state": "closed"})

    final_pr = rest_json("GET", f"/repos/{repo}/pulls/{number}")
    return {
        "ok": True,
        "repo": repo,
        "pr": normalize_pr(final_pr),
        "supersededBy": {"repo": winner_repo, "number": winner_number, "url": winner.get("html_url")},
        "bodyUpdated": body_update is not None,
        "commentUrl": comment.get("html_url") if isinstance(comment, dict) else None,
        "closed": bool(close_result) or pr.get("state") == "closed",
        "neutralizedClosingReferences": replacements,
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


CLOSING_KEYWORDS = (
    "close",
    "closes",
    "closed",
    "fix",
    "fixes",
    "fixed",
    "resolve",
    "resolves",
    "resolved",
)

CLOSING_REFERENCE_RE = re.compile(
    r"\b(" + "|".join(CLOSING_KEYWORDS) + r")\s+((?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#\d+)\b",
    re.IGNORECASE,
)


def neutralize_issue_closing_keywords(body: str) -> tuple[str, list[dict[str, str]]]:
    replacements: list[dict[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        original = match.group(0)
        replacement = f"Refs {match.group(2)}"
        replacements.append({"from": original, "to": replacement})
        return replacement

    return CLOSING_REFERENCE_RE.sub(replace, body), replacements


def pr_reference(current_repo: str, winner_repo: str, winner_number: int, winner_url: Optional[str]) -> str:
    if current_repo == winner_repo:
        return f"#{winner_number}"
    return winner_url or f"{winner_repo}#{winner_number}"


def superseded_comment_body(*, winner_ref: str, reason: Optional[str], body_neutralized: bool, keep_open: bool) -> str:
    verb = "Marking" if keep_open else "Closing"
    parts = [f"{verb} this PR as superseded by {winner_ref}."]
    if reason:
        parts.append(reason.strip())
    if body_neutralized:
        parts.append("Issue-closing references in this PR body were changed to `Refs` so the canonical PR owns issue closure.")
    return "\n\n".join(parts)


FAILURE_CONCLUSIONS = {"failure", "startup_failure", "timed_out", "cancelled", "action_required"}

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


def merge_failure_hint(message: str) -> str:
    lowered = message.lower()
    if "not found" in lowered or "404" in lowered:
        return "GitHub may mask missing merge permission as 404; compare helper token scope with active gh auth."
    if "resource not accessible" in lowered or "forbidden" in lowered or "403" in lowered:
        return "Merge endpoint was denied; check token permissions, branch protection, and required checks."
    if "sha was not found" in lowered or "head branch was modified" in lowered:
        return "PR head changed before merge; refresh PR state and retry with the current head SHA."
    return "Inspect PR state, required checks, branch protection, and helper auth context."


def normalize_pr(pr: dict[str, Any]) -> dict[str, Any]:
    labels = pr.get("labels")
    if not isinstance(labels, list):
        labels = []
    mergeable_state = pr.get("mergeable_state")
    merged = pr.get("merged")
    if merged is None and pr.get("merged_at"):
        merged = True
    return {
        "number": pr.get("number"),
        "title": pr.get("title"),
        "state": pr.get("state"),
        "draft": pr.get("draft"),
        "isDraft": pr.get("draft"),
        "merged": merged,
        "mergeable": pr.get("mergeable"),
        "mergeable_state": mergeable_state,
        "mergeStateStatus": MERGE_STATE_STATUS.get(str(mergeable_state), str(mergeable_state).upper()) if mergeable_state else None,
        "reviewDecision": pr.get("reviewDecision"),
        "statusCheckRollup": pr.get("statusCheckRollup"),
        "labels": labels,
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


def resolve_pr(explicit_repo: Optional[str], value: Optional[str]) -> Tuple[str, int]:
    url_ref = parse_pr_url(value)
    if url_ref:
        return url_ref
    repo = resolve_repo(explicit_repo)
    return repo, resolve_pr_number(repo, value)


def parse_pr_url(value: Optional[str]):
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
    return collect_paged_rest_json(method, path, limit=None, per_page=100)


def limited_paged_rest_json(method: str, path: str, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    items: list[dict[str, Any]] = []
    page = 1
    per_page = min(max(limit, 1), 100)
    while len(items) < limit:
        page_items = collect_single_rest_page(method, path, per_page=per_page, page=page)
        if not page_items:
            break
        items.extend(page_items)
        if len(page_items) < per_page:
            break
        page += 1
    return items[:limit]


def collect_single_rest_page(method: str, path: str, *, per_page: int, page: int) -> list[dict[str, Any]]:
    data = gh_json(["api", "--method", method, *API_VERSION_ARGS, path_with_query(path, {"per_page": per_page, "page": page})])
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def path_with_query(path: str, params: dict[str, Any]) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{urllib.parse.urlencode(params)}"


def collect_paged_rest_json(method: str, path: str, *, limit: Optional[int], per_page: int) -> list[dict[str, Any]]:
    separator = "&" if "?" in path else "?"
    data = gh_json([
        "api",
        "--method",
        method,
        *API_VERSION_ARGS,
        "--paginate",
        "--slurp",
        f"{path}{separator}per_page={per_page}",
    ])
    pages = data if isinstance(data, list) else [data]
    items: list[dict[str, Any]] = []
    for page in pages:
        if isinstance(page, list):
            items.extend(item for item in page if isinstance(item, dict))
            if limit is not None and len(items) >= limit:
                return items[:limit]
            continue
        if isinstance(page, dict):
            for key in ("check_runs", "statuses"):
                value = page.get(key)
                if isinstance(value, list):
                    items.extend(item for item in value if isinstance(item, dict))
                    if limit is not None and len(items) >= limit:
                        return items[:limit]
    return items if limit is None else items[:limit]


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
