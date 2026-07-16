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

import github_api as github_api_core
import github_comment as github_comment_core


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
GH = os.environ.get("GH_PR_GH") or str(SCRIPT_DIR / "gh-with-env-token")
EXPECTED_ACTOR = os.environ.get("GH_WITH_ENV_TOKEN_EXPECTED_LOGIN") or "shiny-code-bot"
CURRENT_OPERATION = "github.pr.unknown"

PR_COMMAND_CONTEXT: dict[str, tuple[str, str, bool]] = {
    "view": ("rest_api", "rest_core", False),
    "list": ("rest_api", "rest_core", False),
    "create": ("gh_cli_wrapper", "mixed", True),
    "edit": ("gh_cli_wrapper", "mixed", True),
    "comment": ("rest_api", "rest_core", True),
    "checks": ("rest_api", "rest_core", False),
    "merge": ("rest_api", "rest_core", True),
    "supersede": ("rest_api", "rest_core", True),
    "rate-limit": ("rest_api", "rest_core", False),
}


class HelperError(Exception):
    pass


class PrHelperError(HelperError):
    def __init__(
        self,
        message: str,
        *,
        failure: Optional[github_api_core.FailureDetail] = None,
        api_result: Optional[dict[str, Any]] = None,
        **payload: Any,
    ):
        super().__init__(github_api_core.redact_string(message))
        self.failure = failure
        if api_result is not None:
            payload["api_result"] = api_result
        self.payload = github_api_core.redact_body(payload)


def main() -> int:
    global CURRENT_OPERATION
    try:
        args = parse_args()
    except github_api_core.ArgumentParsingError as exc:
        command = github_api_core.requested_subcommand(sys.argv[1:], set(PR_COMMAND_CONTEXT))
        CURRENT_OPERATION = f"github.pr.{command.replace('-', '_')}"
        transport, bucket, is_write = PR_COMMAND_CONTEXT.get(command, ("helper", "unknown", False))
        failure = github_api_core.FailureDetail(
            cause="validation_error",
            message=str(exc),
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome="not_started" if is_write else None,
            failed_step="argument_parsing",
        )
        return github_api_core.emit_terminal(
            github_api_core.terminal_failure(
                failure,
                operation=CURRENT_OPERATION,
                expected_actor=EXPECTED_ACTOR,
                transport=transport,
                bucket=bucket,
                exit_code=2,
                failed_step="argument_parsing",
            ),
            stderr_message=f"error: {exc}",
        )
    CURRENT_OPERATION = f"github.pr.{args.command.replace('-', '_')}"
    transport, bucket, is_write = PR_COMMAND_CONTEXT.get(args.command, ("helper", "unknown", False))
    try:
        payload = args.func(args)
    except PrHelperError as exc:
        failure = exc.failure or github_api_core.FailureDetail(
            cause="helper_error",
            message=str(exc),
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome="not_started" if is_write else None,
        )
        api_result = exc.payload.get("api_result") if isinstance(exc.payload.get("api_result"), dict) else {}
        envelope = github_api_core.terminal_failure(
            failure,
            operation=CURRENT_OPERATION,
            payload=exc.payload,
            actor=api_result.get("actor"),
            expected_actor=(
                api_result["expected_actor"]
                if "expected_actor" in api_result
                else EXPECTED_ACTOR
            ),
            host=api_result.get("host") or github_api_core.DEFAULT_HOST,
            transport=api_result.get("transport") or transport,
            bucket=api_result.get("bucket") or bucket,
            status=int(api_result.get("status") or 0),
            request_id=api_result.get("request_id"),
            completed_steps=api_result.get("completed_steps") or failure.completed_steps,
            failed_step=api_result.get("failed_step") or failure.failed_step,
            error=str(exc),
        )
        for key in ("quota", "rate_limit", "retry_at", "retry_after", "write_outcome", "disposition"):
            if api_result.get(key) is not None:
                envelope[key] = api_result[key]
        return github_api_core.emit_terminal(
            envelope,
            stderr_message=f"error: {exc}",
        )
    except HelperError as exc:
        message = github_api_core.redact_string(str(exc))
        failure = github_api_core.FailureDetail(
            cause="validation_error",
            message=message,
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome="not_started" if is_write else None,
        )
        return github_api_core.emit_terminal(
            github_api_core.terminal_failure(
                failure,
                operation=CURRENT_OPERATION,
                expected_actor=EXPECTED_ACTOR,
                transport=transport,
                bucket=bucket,
                error=message,
            ),
            stderr_message=f"error: {message}",
        )
    actor = payload.get("actor") if isinstance(payload, dict) else None
    expected_actor = payload.get("expected_actor", EXPECTED_ACTOR) if isinstance(payload, dict) else EXPECTED_ACTOR
    completed_steps = payload.get("completed_steps") if isinstance(payload, dict) else None
    return github_api_core.emit_terminal(
        github_api_core.terminal_success(
            payload,
            operation=CURRENT_OPERATION,
            actor=actor,
            expected_actor=expected_actor,
            transport=transport,
            bucket=bucket,
            completed_steps=completed_steps if isinstance(completed_steps, list) else None,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = github_api_core.TerminalArgumentParser(
        description="Handle GitHub PR reads, writes, checks, merge, and rate limits with transport-aware defaults.",
    )
    parser.add_argument("--repo", help="Repository in OWNER/REPO form.")
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=github_api_core.TerminalArgumentParser,
    )

    p = sub.add_parser("view", help="Show PR metadata via REST.")
    p.add_argument("pr", nargs="?", help="PR number or URL. Defaults to current branch PR.")
    p.set_defaults(func=cmd_view)

    p = sub.add_parser("list", help="List pull requests via REST.")
    p.add_argument("--state", choices=("open", "closed", "all"), default="open")
    p.add_argument("--limit", type=int, default=20, help="Maximum PRs to return.")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("create", help="Create a PR through gh-with-env-token.")
    p.add_argument("--title", help="PR title. Required unless --fill, --fill-first, or --fill-verbose is used.")
    p.add_argument("--body-file", help="Read PR body text from this file. Use '-' to read from stdin.")
    p.add_argument("--base", help="Base branch for the PR.")
    p.add_argument("--head", help="Head branch for the PR.")
    p.add_argument("--draft", action="store_true", help="Open the PR as a draft.")
    p.add_argument("--dry-run", action="store_true", help="Print details instead of creating the PR.")
    p.add_argument("--fill", action="store_true", help="Use commit info for title and body.")
    p.add_argument("--fill-first", action="store_true", help="Use first commit info for title and body.")
    p.add_argument("--fill-verbose", action="store_true", help="Use commits message and body for PR content.")
    p.add_argument("--label", action="append", default=[], help="Add a label by name. Repeat for multiple labels.")
    p.add_argument("--reviewer", action="append", default=[], help="Request a reviewer. Repeat for multiple reviewers.")
    p.add_argument("--assignee", action="append", default=[], help="Assign a user. Repeat for multiple assignees.")
    p.add_argument("--milestone", help="Add the PR to a milestone by name.")
    p.add_argument("--project", action="append", default=[], help="Add the PR to a project. Repeat for multiple projects.")
    p.add_argument("--template", help="Template file to use as starting body text.")
    p.add_argument("--no-maintainer-edit", action="store_true", help="Disable maintainer edits on the head branch.")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("edit", help="Edit PR metadata or body through gh-with-env-token.")
    p.add_argument("pr", nargs="?", help="PR number, URL, or branch. Defaults to current branch PR.")
    p.add_argument("--title", help="Set the PR title.")
    p.add_argument("--body-file", help="Read the replacement PR body from this file. Use '-' to read from stdin.")
    p.add_argument("--base", help="Change the PR base branch.")
    p.add_argument("--add-label", action="append", default=[], help="Add labels by name. Repeat for multiple entries.")
    p.add_argument("--remove-label", action="append", default=[], help="Remove labels by name. Repeat for multiple entries.")
    p.add_argument("--add-reviewer", action="append", default=[], help="Add or re-request reviewers. Repeat for multiple entries.")
    p.add_argument("--remove-reviewer", action="append", default=[], help="Remove reviewers. Repeat for multiple entries.")
    p.add_argument("--add-assignee", action="append", default=[], help="Add assignees. Repeat for multiple entries.")
    p.add_argument("--remove-assignee", action="append", default=[], help="Remove assignees. Repeat for multiple entries.")
    p.add_argument("--milestone", help="Set the PR milestone by name.")
    p.add_argument("--remove-milestone", action="store_true", help="Remove the PR milestone.")
    p.add_argument("--add-project", action="append", default=[], help="Add the PR to projects. Repeat for multiple entries.")
    p.add_argument("--remove-project", action="append", default=[], help="Remove the PR from projects. Repeat for multiple entries.")
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("comment", help="Add a PR timeline comment through the shared REST path.")
    p.add_argument("pr", help="PR number, URL, or branch.")
    p.add_argument("--body-file", required=True, help="Read comment Markdown from this file. Use '-' to read from stdin.")
    p.add_argument("--edit-last", action="store_true", help="Edit the authenticated actor's latest comment.")
    p.add_argument("--create-if-none", action="store_true", help="Create a comment when --edit-last finds none.")
    p.set_defaults(func=cmd_comment)

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
    p.add_argument("--delete-branch", action="store_true", help="Delete the superseded PR's same-repo remote branch when safe.")
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


def cmd_create(args: argparse.Namespace) -> dict[str, Any]:
    repo = resolve_repo(args.repo)
    fill_flags = [args.fill, args.fill_first, args.fill_verbose]
    if not args.title and not any(fill_flags):
        raise PrHelperError("PR create requires --title unless a --fill variant is used", operation="create", repo=repo)
    if not args.body_file and not any(fill_flags):
        raise PrHelperError("PR create requires --body-file unless a --fill variant is used", operation="create", repo=repo)
    if sum(1 for value in fill_flags if value) > 1:
        raise PrHelperError("Use only one of --fill, --fill-first, or --fill-verbose", operation="create", repo=repo)
    gh_args = ["pr", "create", "--repo", repo]
    append_flag(gh_args, "--title", args.title)
    append_flag(gh_args, "--body-file", args.body_file)
    append_flag(gh_args, "--base", args.base)
    append_flag(gh_args, "--head", args.head)
    append_bool(gh_args, "--draft", args.draft)
    append_bool(gh_args, "--dry-run", args.dry_run)
    append_bool(gh_args, "--fill", args.fill)
    append_bool(gh_args, "--fill-first", args.fill_first)
    append_bool(gh_args, "--fill-verbose", args.fill_verbose)
    append_repeated_flag(gh_args, "--label", args.label)
    append_repeated_flag(gh_args, "--reviewer", args.reviewer)
    append_repeated_flag(gh_args, "--assignee", args.assignee)
    append_flag(gh_args, "--milestone", args.milestone)
    append_repeated_flag(gh_args, "--project", args.project)
    append_flag(gh_args, "--template", args.template)
    append_bool(gh_args, "--no-maintainer-edit", args.no_maintainer_edit)
    proc = run_pr_write(gh_args, operation="create", repo=repo)
    stdout = proc.stdout.strip()
    return {"ok": True, "operation": "create", "repo": repo, "url": extract_url(stdout), "stdout": stdout}


def cmd_edit(args: argparse.Namespace) -> dict[str, Any]:
    repo = resolve_repo(args.repo)
    gh_args = ["pr", "edit"]
    if args.pr:
        gh_args.append(args.pr)
    gh_args.extend(["--repo", repo])
    append_flag(gh_args, "--title", args.title)
    append_flag(gh_args, "--body-file", args.body_file)
    append_flag(gh_args, "--base", args.base)
    append_repeated_flag(gh_args, "--add-label", args.add_label)
    append_repeated_flag(gh_args, "--remove-label", args.remove_label)
    append_repeated_flag(gh_args, "--add-reviewer", args.add_reviewer)
    append_repeated_flag(gh_args, "--remove-reviewer", args.remove_reviewer)
    append_repeated_flag(gh_args, "--add-assignee", args.add_assignee)
    append_repeated_flag(gh_args, "--remove-assignee", args.remove_assignee)
    append_flag(gh_args, "--milestone", args.milestone)
    append_bool(gh_args, "--remove-milestone", args.remove_milestone)
    append_repeated_flag(gh_args, "--add-project", args.add_project)
    append_repeated_flag(gh_args, "--remove-project", args.remove_project)
    if len(gh_args) <= (5 if args.pr else 4):
        raise PrHelperError("PR edit requires at least one edit flag", operation="edit", repo=repo, pr=args.pr)
    proc = run_pr_write(gh_args, operation="edit", repo=repo, pr=args.pr)
    return {"ok": True, "operation": "edit", "repo": repo, "pr": args.pr, "stdout": proc.stdout.strip()}


def shared_comment(
    kind: str,
    repo: str,
    number: int,
    body: str,
    *,
    operation: str,
    completed_steps: Optional[list[str]] = None,
    failed_step: Optional[str] = None,
    edit_last: bool = False,
    create_if_none: bool = False,
) -> dict[str, Any]:
    try:
        return github_comment_core.comment(
            kind,
            number,
            body,
            repo=repo,
            gh_cmd=GH,
            expected_actor=EXPECTED_ACTOR,
            operation=operation,
            completed_steps=completed_steps,
            failed_step=failed_step,
            edit_last=edit_last,
            create_if_none=create_if_none,
        )
    except github_comment_core.CommentError as exc:
        raise PrHelperError(
            str(exc),
            failure=exc.failure,
            api_result=exc.api_result,
            **exc.payload,
        ) from exc


def cmd_comment(args: argparse.Namespace) -> dict[str, Any]:
    repo, number = resolve_pr(args.repo, args.pr)
    body = read_text_file(args.body_file, operation="comment", repo=repo, pr=number)
    if not body:
        raise PrHelperError("PR comment body is empty", operation="comment", repo=repo, pr=number)
    result = shared_comment(
        "pr",
        repo,
        number,
        body,
        operation=CURRENT_OPERATION,
        edit_last=args.edit_last,
        create_if_none=args.create_if_none,
    )
    return {
        **result,
        "ok": True,
        "operation": "comment",
        "pr": args.pr,
        "stdout": result.get("url") or "",
    }


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
        api_result = exc.payload.get("api_result") if isinstance(exc, PrHelperError) else None
        raise PrHelperError(
            "PR merge failed",
            failure=exc.failure if isinstance(exc, PrHelperError) else None,
            detail=str(exc),
            operation="merge",
            repo=repo,
            pr=number,
            endpoint=merge_path,
            method=args.method,
            headSha=pr["head"]["sha"],
            hint=merge_failure_hint(str(exc)),
            api_result=api_result,
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
    cleanup_warnings: list[dict[str, str]] = []
    planned_branch_delete = None
    if args.delete_branch and planned_close:
        repo_metadata = rest_json("GET", f"/repos/{repo}")
        same_head_open_prs = same_head_open_pull_requests(repo, pr)
        planned_branch_delete, cleanup_warnings = superseded_branch_delete_plan(
            pr,
            winner,
            repo,
            repo_metadata,
            same_head_open_prs,
        )

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
                "deleteBranch": planned_branch_delete,
            },
            "cleanupWarnings": cleanup_warnings,
            "neutralizedClosingReferences": replacements,
        }

    close_result = None
    if planned_close and pr.get("state") != "closed":
        close_result = rest_json("PATCH", f"/repos/{repo}/pulls/{number}", {"state": "closed"})

    completed_steps = ["close_pull_request"] if close_result is not None else []
    comment = shared_comment(
        "pr",
        repo,
        number,
        comment_body,
        operation=CURRENT_OPERATION,
        completed_steps=completed_steps,
        failed_step="post_supersede_comment",
    )

    body_update = None
    body_update_error = None
    if replacements:
        try:
            body_update = rest_json("PATCH", f"/repos/{repo}/pulls/{number}", {"body": updated_body})
        except HelperError as exc:
            body_update_error = str(exc)

    deleted = None
    if planned_branch_delete:
        deleted = delete_ref(repo, planned_branch_delete["ref"])
    cleanup_warnings.extend(cleanup_warnings_for_deleted_branch(deleted))
    if body_update_error:
        cleanup_warnings.append(
            {
                "kind": "body_update_failed",
                "reason": "Supersede close/comment succeeded, but the PR body could not be rewritten to neutralize closing keywords.",
                "stderr": body_update_error,
            }
        )

    final_pr = rest_json("GET", f"/repos/{repo}/pulls/{number}")
    return {
        "ok": True,
        "repo": repo,
        "pr": normalize_pr(final_pr),
        "supersededBy": {"repo": winner_repo, "number": winner_number, "url": winner.get("html_url")},
        "bodyUpdated": body_update is not None,
        "commentUrl": comment.get("url"),
        "closed": bool(close_result) or pr.get("state") == "closed",
        "deletedBranch": deleted,
        "cleanupWarnings": cleanup_warnings,
        "neutralizedClosingReferences": replacements,
        "completed_steps": comment.get("completed_steps", []),
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

CLOSING_ISSUE_URL_RE = re.compile(
    r"\b("
    + "|".join(CLOSING_KEYWORDS)
    + r")\s+(https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d+))(?:\b|(?=[?#]))",
    re.IGNORECASE,
)


def neutralize_issue_closing_keywords(body: str) -> tuple[str, list[dict[str, str]]]:
    replacements: list[dict[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        original = match.group(0)
        replacement = f"Refs {match.group(2)}"
        replacements.append({"from": original, "to": replacement})
        return replacement

    def replace_url(match: re.Match[str]) -> str:
        original = match.group(0)
        replacement = f"Refs {match.group(3)}#{match.group(4)}"
        replacements.append({"from": original, "to": replacement})
        return replacement

    body = CLOSING_ISSUE_URL_RE.sub(replace_url, body)
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


def superseded_branch_delete_plan(
    pr: dict[str, Any],
    winner: dict[str, Any],
    repo: str,
    repo_metadata: dict[str, Any],
    same_head_open_prs: list[dict[str, Any]],
) -> tuple[Optional[dict[str, str]], list[dict[str, str]]]:
    head = pr.get("head") or {}
    head_repo = head.get("repo") or {}
    winner_head = winner.get("head") or {}
    winner_head_repo = winner_head.get("repo") or {}
    base = pr.get("base") or {}
    head_full_name = head_repo.get("full_name")
    head_ref = head.get("ref")
    winner_head_full_name = winner_head_repo.get("full_name")
    winner_head_ref = winner_head.get("ref")
    base_ref = base.get("ref")
    default_branch = repo_metadata.get("default_branch")
    if head_full_name != repo or not head_ref or head_ref == base_ref:
        return None, []
    if head_ref == default_branch:
        return None, [
            {
                "kind": "remote_branch_delete_skipped_shared_ref",
                "ref": f"heads/{head_ref}",
                "reason": "Superseded PR head branch is the repository default branch.",
            }
        ]
    if head_full_name == winner_head_full_name and head_ref == winner_head_ref:
        return None, [
            {
                "kind": "remote_branch_delete_skipped_active_pr",
                "ref": f"heads/{head_ref}",
                "reason": "Superseded and canonical PRs share the same head branch.",
            }
        ]
    dependent_prs = [item for item in same_head_open_prs if item.get("number") != pr.get("number")]
    if dependent_prs:
        return None, [
            {
                "kind": "remote_branch_delete_skipped_active_pr",
                "ref": f"heads/{head_ref}",
                "reason": "Another open PR uses the superseded PR head branch.",
                "pullRequests": ",".join(str(item.get("number")) for item in dependent_prs if item.get("number")),
            }
        ]
    return {"repo": repo, "branch": head_ref, "ref": f"heads/{head_ref}"}, []


def same_head_open_pull_requests(repo: str, pr: dict[str, Any]) -> list[dict[str, Any]]:
    head = pr.get("head") or {}
    head_repo = head.get("repo") or {}
    head_full_name = head_repo.get("full_name")
    head_ref = head.get("ref")
    if head_full_name != repo or not head_ref:
        return []
    owner = repo.split("/", 1)[0]
    return limited_paged_rest_json(
        "GET",
        f"/repos/{repo}/pulls?state=open&head={urllib.parse.quote(f'{owner}:{head_ref}', safe='')}",
        100,
    )


def cleanup_warnings_for_deleted_branch(deleted: Optional[dict[str, Any]]) -> list[dict[str, str]]:
    if not deleted or deleted.get("deleted"):
        return []
    return [
        {
            "kind": "remote_branch_delete_failed",
            "ref": str(deleted.get("ref") or ""),
            "stderr": str(deleted.get("stderr") or ""),
        }
    ]


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
        pulls = rest_json(
            "GET",
            f"/repos/{repo}/pulls",
            params={"head": f"{repo.split('/')[0]}:{value}", "state": "open"},
        )
        if isinstance(pulls, list) and len(pulls) == 1:
            return int(pulls[0]["number"])
        if isinstance(pulls, list) and len(pulls) > 1:
            raise HelperError(f"Multiple open PRs found for branch {value}; pass a PR number")
        raise HelperError(f"No open PR found for branch {value}; pass a PR number")
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
    return rest_result(method, path, payload, params=params).body


def rest_result(
    method: str,
    path: str,
    payload: Optional[dict] = None,
    *,
    params: Optional[dict] = None,
) -> github_api_core.ApiResult:
    if params:
        path = path_with_query(path, params)
    result = github_api_core.call_gh(
        method,
        path,
        payload,
        gh_cmd=GH,
        operation=CURRENT_OPERATION,
        expected_actor=EXPECTED_ACTOR,
        bucket="rest_core",
    )
    if not result.ok:
        detail = result.failure.message if result.failure else "GitHub API request failed"
        raise PrHelperError(
            detail,
            failure=result.failure,
            method=method.upper(),
            endpoint=github_api_core.redact_path(path),
            api_result=result.as_dict(),
        )
    return result


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
    data = rest_json(method, path, params={"per_page": per_page, "page": page})
    return rest_page_items(data)


def path_with_query(path: str, params: dict[str, Any]) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{urllib.parse.urlencode(params)}"


def collect_paged_rest_json(method: str, path: str, *, limit: Optional[int], per_page: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    next_path: Optional[str] = path_with_query(path, {"per_page": per_page})
    while next_path:
        result = rest_result(method, next_path)
        items.extend(rest_page_items(result.body))
        if limit is not None and len(items) >= limit:
            return items[:limit]
        next_path = next_link(result.headers.get("link"))
    return items if limit is None else items[:limit]


def rest_page_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("check_runs", "statuses", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


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


def delete_ref(repo: str, ref: str) -> dict[str, Any]:
    result = github_api_core.call_gh(
        "DELETE",
        f"/repos/{repo}/git/refs/{ref}",
        gh_cmd=GH,
        operation="gh-pr.delete-ref",
    )
    deleted = {"ref": ref, "deleted": result.ok, "stderr": ""}
    if not result.ok:
        deleted["stderr"] = result.failure.message if result.failure else "GitHub API request failed"
        deleted["api_result"] = result.as_dict()
    return deleted


def append_flag(args: list[str], flag: str, value: Optional[str]) -> None:
    if value is not None:
        args.extend([flag, value])


def append_bool(args: list[str], flag: str, enabled: bool) -> None:
    if enabled:
        args.append(flag)


def append_repeated_flag(args: list[str], flag: str, values: list[str]) -> None:
    for value in values:
        args.extend([flag, value])


def read_text_file(path: str, **payload: Any) -> str:
    try:
        if path == "-":
            return sys.stdin.read()
        return pathlib.Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise PrHelperError("Could not read body file", detail=str(exc), path=path, **payload) from exc


def run_pr_write(args: list[str], **payload: Any) -> subprocess.CompletedProcess[str]:
    proc = run_gh(args)
    if proc.returncode != 0:
        operation = f"github.pr.{str(payload.get('operation') or 'write').replace('-', '_')}"
        result = github_api_core.legacy_process_result(
            proc.returncode,
            proc.stdout,
            proc.stderr,
            operation=operation,
            is_write=True,
            expected_actor=EXPECTED_ACTOR,
            transport="gh_cli_wrapper",
            bucket="mixed",
        )
        raise PrHelperError(
            "PR write failed",
            failure=result.failure,
            api_result=result.as_dict(),
            detail=(proc.stderr or proc.stdout or "gh pr write failed").strip(),
            command=args[:3],
            **payload,
        )
    return proc


def extract_url(value: str) -> Optional[str]:
    match = re.search(r"https?://\S+", value)
    return match.group(0) if match else None


def gh_json(args: list[str]) -> Any:
    proc = run_gh(args)
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "gh command failed").strip()
        result = github_api_core.legacy_process_result(
            proc.returncode,
            proc.stdout,
            proc.stderr,
            operation=CURRENT_OPERATION,
            is_write=False,
            expected_actor=EXPECTED_ACTOR,
            transport="gh_cli_wrapper",
            bucket="mixed",
        )
        raise PrHelperError(
            message,
            failure=result.failure,
            command=args[:2],
            api_result=result.as_dict(),
        )
    if not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise PrHelperError("Expected JSON from gh", detail=proc.stdout[:300], command=args[:2]) from exc


def run_gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([GH, *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def run_git(args: list[str]) -> str:
    proc = subprocess.run(["git", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise HelperError(proc.stderr.strip() or "git command failed")
    return proc.stdout


if __name__ == "__main__":
    raise SystemExit(main())
