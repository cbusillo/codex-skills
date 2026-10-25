#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
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
import tempfile
import time
import urllib.parse
from typing import Any, Callable, Optional, Tuple

import github_api as github_api_core
import github_comment as github_comment_core
import github_read as github_read_core
import github_identity
import github_plan_landing
import github_plan_status.references
import github_plan_wait
import director_question_gate
from ready_handoff import (
    client_handoff,
    handoff_body,
    review_evidence_gap,
    review_result_body,
)


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
GH = os.environ.get("GH_PR_GH") or str(SCRIPT_DIR / "gh-with-env-token")
EXPECTED_ACTOR = github_identity.automation_login()
NO_OWNER = "no owner"
CURRENT_OPERATION = "github.pr.unknown"
CURRENT_RETRY_FIELDS: dict[str, Any] = {}
CURRENT_RETRY_SUMMARY: Optional[github_api_core.RetrySummary] = None
FULL_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")

PR_COMMAND_CONTEXT: dict[str, tuple[str, str, bool]] = {
    "view": ("rest_api", "rest_core", False),
    "list": ("rest_api", "rest_core", False),
    "create": ("gh_cli_wrapper", "mixed", True),
    "edit": ("gh_cli_wrapper", "mixed", True),
    "comment": ("rest_api", "rest_core", True),
    "ready": ("rest_api", "rest_core", True),
    "checks": ("rest_api", "rest_core", False),
    "merge": ("rest_api", "rest_core", True),
    "update-branch": ("rest_api", "rest_core", True),
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


def record_retry_fields(result: github_api_core.ApiResult) -> None:
    record_retry_summary(result.retry_summary)


def record_retry_summary(summary: Optional[github_api_core.RetrySummary]) -> None:
    global CURRENT_RETRY_FIELDS, CURRENT_RETRY_SUMMARY
    if summary is None:
        return
    CURRENT_RETRY_SUMMARY = github_api_core.aggregate_retry_summaries(
        [item for item in (CURRENT_RETRY_SUMMARY, summary) if item is not None]
    )
    CURRENT_RETRY_FIELDS = CURRENT_RETRY_SUMMARY.as_dict() if CURRENT_RETRY_SUMMARY else {}


def main() -> int:
    global CURRENT_OPERATION, CURRENT_RETRY_FIELDS, CURRENT_RETRY_SUMMARY
    CURRENT_RETRY_FIELDS = {}
    CURRENT_RETRY_SUMMARY = None
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
        for key in (
            "quota",
            "rate_limit",
            "retry_at",
            "retry_after",
            "write_outcome",
            "disposition",
            *github_api_core.RETRY_TERMINAL_KEYS,
        ):
            if api_result.get(key) is not None:
                envelope[key] = api_result[key]
        if CURRENT_RETRY_FIELDS:
            envelope.update(CURRENT_RETRY_FIELDS)
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
        envelope = github_api_core.terminal_failure(
            failure,
            operation=CURRENT_OPERATION,
            expected_actor=EXPECTED_ACTOR,
            transport=transport,
            bucket=bucket,
            error=message,
        )
        if CURRENT_RETRY_FIELDS:
            envelope.update(CURRENT_RETRY_FIELDS)
        return github_api_core.emit_terminal(
            envelope,
            stderr_message=f"error: {message}",
        )
    if isinstance(payload, dict) and CURRENT_RETRY_FIELDS:
        payload = {**payload, **CURRENT_RETRY_FIELDS}
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


def build_parser() -> argparse.ArgumentParser:
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
    p.add_argument("--attach", action="append", help="Attach FILE[#alt] through GitHub CLI; repeatable (images/videos only).")
    p.add_argument("--reviewed-head", help="Attest that this disposition weighs the completed review for this full head SHA.")
    p.add_argument("--edit-last", action="store_true", help="Edit the authenticated actor's latest comment.")
    p.add_argument("--edit-comment", type=int, help="Edit this exact timeline comment ID.")
    p.add_argument("--expected-body-file", help="Require this exact prior comment body.")
    p.add_argument("--expected-updated-at", help="Require this prior updated_at value.")
    p.add_argument("--create-if-none", action="store_true", help="Create a comment when --edit-last finds none.")
    p.set_defaults(func=cmd_comment)

    p = sub.add_parser("ready", help="Post the current-head merge-train handoff after checking engineering readiness.")
    p.add_argument("pr", help="PR number, URL, or branch.")
    p.add_argument("--waiting-for-client", action="store_true", help="Post Engineering ready; waiting for the Client's acceptance. Only pending launchplane/owner-review checks may remain.")
    p.add_argument("--allow-no-checks", action="store_true", help="Permit an empty check inventory only when the repository has no CI checks configured.")
    p.set_defaults(func=cmd_ready)

    p = sub.add_parser("checks", help="Show check runs and commit statuses via REST.")
    p.add_argument("pr", nargs="?", help="PR number or URL. Defaults to current branch PR.")
    p.set_defaults(func=cmd_checks)

    p = sub.add_parser("update-branch", help="Update a PR with its base through the automation identity.")
    p.add_argument("pr", help="PR number or URL.")
    p.add_argument("--wait-seconds", type=int, choices=range(0, 61), default=30, metavar="0..60",
                   help="Seconds to observe the asynchronous update (default: 30).")
    p.add_argument("--required-by", metavar="REQUIREMENT",
                   help="The check or rule that requires a fresh base when GitHub does not report the PR behind.")
    p.set_defaults(func=cmd_update_branch)

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

    return parser


def parse_args() -> argparse.Namespace:
    return build_parser().parse_args()


def read_operation() -> str:
    """Keep delegated polling in its matrix-owned bulk context.

    Only view/checks can inherit watch context. Write commands and their
    preflights retain their own essential operation regardless of this hint.
    """
    if CURRENT_OPERATION in {"github.pr.view", "github.pr.checks"} and os.environ.get("GH_PR_READ_CONTEXT") == "watch":
        return "github.pr.watch"
    return CURRENT_OPERATION


def review_decision_from_reviews(reviews: list[dict[str, Any]]) -> Optional[str]:
    ordered = sorted(
        enumerate(reviews),
        key=lambda pair: (
            0 if isinstance(pair[1], dict) and isinstance(pair[1].get("id"), int) else 1,
            pair[1].get("id") if isinstance(pair[1], dict) and isinstance(pair[1].get("id"), int) else pair[0],
            pair[0],
        ),
    )
    by_author: dict[str, str] = {}
    for _, item in ordered:
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or "").upper()
        if state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        association = str(item.get("author_association") or "").strip().upper()
        if association == "NONE":
            continue
        user = item.get("user")
        if not isinstance(user, dict):
            user = {}
        login = str(user.get("login") or "").strip().lower()
        if not login:
            continue
        by_author[login] = state
    if any(state == "CHANGES_REQUESTED" for state in by_author.values()):
        return "CHANGES_REQUESTED"
    if any(state == "APPROVED" for state in by_author.values()):
        return "APPROVED"
    return None


def cmd_view(args: argparse.Namespace, *, cache_enabled: bool = True, include_body: bool = False) -> dict[str, Any]:
    repo, number = resolve_pr(args.repo, args.pr)
    operation = read_operation()
    reader = github_read_core.GitHubReader(
        gh_cmd=GH, expected_actor=EXPECTED_ACTOR, operation=operation,
        cache_enabled=cache_enabled,
    )
    try:
        pr = reader.get_json(f"/repos/{repo}/pulls/{number}", step="pull_request")
    except github_read_core.GitHubReadError as exc:
        raise PrHelperError(str(exc), failure=exc.result.failure, api_result=exc.result.as_dict(), diagnostics=exc.diagnostics, repo=repo, pr=number) from exc
    if not isinstance(pr, dict):
        raise PrHelperError("PR metadata response was not an object", diagnostics=reader.diagnostics(), repo=repo, pr=number)
    reviews_read = pr.get("reviewDecision") is not None
    reviews_read_failed = False
    decision_source = "pull_request_metadata" if reviews_read else None
    if not include_body and not reviews_read and operation != "github.pr.watch":
        try:
            reviews = reader.paged_json(f"/repos/{repo}/pulls/{number}/reviews", step_prefix="reviews")
            reviews_read = True
        except github_read_core.GitHubReadError as exc:
            if isinstance(exc.result.failure, dict) and exc.result.failure.get("disposition") == "stop":
                raise PrHelperError(
                    str(exc), failure=exc.result.failure, api_result=exc.result.as_dict(),
                    diagnostics=exc.diagnostics, repo=repo, pr=number,
                ) from exc
            reviews = []
            reviews_read_failed = True
        except github_read_core.GitHubReadShapeError:
            reviews = []
            reviews_read_failed = True
        decision = review_decision_from_reviews(reviews)
        if decision:
            pr = {**pr, "reviewDecision": decision}
            decision_source = "rest_reviews"
    record_retry_summary(reader.retry_summary())
    normalized = normalize_pr(pr)
    if include_body:
        normalized["body"] = pr.get("body") or ""
        if isinstance(pr.get("user"), dict):
            normalized["user"] = pr["user"]
    has_review_decision = normalized.get("reviewDecision") is not None
    readiness: dict[str, Any] = {
        "state": "not_read",
        "review_decision": "read" if has_review_decision else "not_read",
    }
    if has_review_decision and decision_source:
        readiness["review_decision_source"] = decision_source
    elif not has_review_decision:
        if reviews_read_failed:
            readiness["reason"] = "review decision read failed; see diagnostics"
        elif reviews_read:
            readiness["reason"] = "no decisive review on pull request; review requirement not read by view"
        else:
            readiness["reason"] = "review decision not read by view"
    return {
        "ok": True, "repo": repo, "pr": normalized,
        "actor": reader.actor, "expected_actor": reader.expected_actor,
        **merge_observations(pr, readiness=readiness), "diagnostics": reader.diagnostics(),
    }


def cmd_list(args: argparse.Namespace) -> dict[str, Any]:
    repo = resolve_repo(args.repo)
    limit = max(args.limit, 0)
    pulls = limited_paged_rest_json("GET", f"/repos/{repo}/pulls?state={args.state}", limit)
    return {"ok": True, "repo": repo, "pullRequests": [normalize_pr(item) for item in pulls]}


# The owner's voice, for a PR opened as the person in a repository they do not
# control; it claims review, not testing, which only the person can say.
AI_ASSISTANCE_SENTENCE = "I wrote this change with AI assistance and reviewed it."


def cmd_create(args: argparse.Namespace) -> dict[str, Any]:
    repo = resolve_repo(args.repo)
    fill_flags = [args.fill, args.fill_first, args.fill_verbose]
    if not args.title and not any(fill_flags):
        raise PrHelperError("PR create requires --title unless a --fill variant is used", operation="create", repo=repo)
    if not args.body_file and not any(fill_flags):
        raise PrHelperError("PR create requires --body-file unless a --fill variant is used", operation="create", repo=repo)
    if sum(1 for value in fill_flags if value) > 1:
        raise PrHelperError("Use only one of --fill, --fill-first, or --fill-verbose", operation="create", repo=repo)
    body_file = args.body_file
    ai_sentence_added = False
    own_user = github_identity.acts_as_own_user(repo)
    if own_user:
        # The person opens this PR in a repository they do not control, so its
        # body says the change was written with AI assistance.
        if not args.body_file:
            raise PrHelperError(
                "PR create as your own GitHub user requires --body-file so the AI-assistance sentence can be added",
                operation="create",
                repo=repo,
            )
        body = read_text_file(args.body_file, operation="create", repo=repo)
        if "ai assistance" not in body.casefold():
            body = f"{body.rstrip()}\n\n{AI_ASSISTANCE_SENTENCE}\n"
            ai_sentence_added = True
        body_path = pathlib.Path(tempfile.mkstemp(prefix="gh-pr-body-", suffix=".md")[1])
        body_path.write_text(body, encoding="utf-8")
        body_file = str(body_path)
    gh_args = ["pr", "create", "--repo", repo]
    append_flag(gh_args, "--title", args.title)
    append_flag(gh_args, "--body-file", body_file)
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
    try:
        proc = run_pr_write(gh_args, operation="create", repo=repo)
    finally:
        if body_file != args.body_file:
            pathlib.Path(body_file).unlink(missing_ok=True)
    stdout = proc.stdout.strip()
    result = {"ok": True, "operation": "create", "repo": repo, "url": extract_url(stdout), "stdout": stdout}
    if own_user:
        result["identity"] = "own_user"
        result["aiAssistanceSentenceAdded"] = ai_sentence_added
    return result


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
    edit_comment: Optional[int] = None,
    expected_body: Optional[str] = None,
    expected_updated_at: Optional[str] = None,
    create_if_none: bool = False,
    attachments: list[str] | None = None,
) -> dict[str, Any]:
    retry_summaries: list[github_api_core.RetrySummary] = []
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
            edit_comment=edit_comment,
            expected_body=expected_body,
            expected_updated_at=expected_updated_at,
            create_if_none=create_if_none,
            attachments=attachments,
            retry_summaries=retry_summaries,
        )
    except github_comment_core.CommentError as exc:
        raise PrHelperError(
            str(exc),
            failure=exc.failure,
            api_result=exc.api_result,
            **exc.payload,
        ) from exc
    finally:
        for summary in retry_summaries:
            record_retry_summary(summary)


def cmd_comment(args: argparse.Namespace) -> dict[str, Any]:
    repo, number = resolve_pr(args.repo, args.pr)
    body = read_text_file(args.body_file, operation="comment", repo=repo, pr=number)
    if not body:
        raise PrHelperError("PR comment body is empty", operation="comment", repo=repo, pr=number)
    reviewed_head = getattr(args, "reviewed_head", None)
    if reviewed_head is not None:
        if args.edit_last or args.edit_comment is not None or args.create_if_none:
            raise PrHelperError("Post a new review disposition comment; do not edit an earlier result", operation="comment", repo=repo, pr=number)
        try:
            body = review_result_body(body, reviewed_head)
        except ValueError as exc:
            raise PrHelperError(str(exc), operation="comment", repo=repo, pr=number) from exc
    result = shared_comment(
        "pr",
        repo,
        number,
        body,
        operation=CURRENT_OPERATION,
        edit_last=args.edit_last,
        edit_comment=args.edit_comment,
        expected_body=read_text_file(args.expected_body_file, preserve_newlines=True, operation="comment", repo=repo, pr=number) if args.expected_body_file is not None else None,
        expected_updated_at=args.expected_updated_at,
        create_if_none=args.create_if_none,
        attachments=getattr(args, "attach", None),
    )
    return {
        **result,
        "ok": True,
        "operation": "comment",
        "pr": args.pr,
        "stdout": result.get("url") or "",
    }


def cmd_ready(args: argparse.Namespace) -> dict[str, Any]:
    repo, number = resolve_pr(args.repo, args.pr)
    target = argparse.Namespace(repo=repo, pr=str(number))
    checks = cmd_checks(target)
    pr = checks["pr"]
    summary = checks["summary"]
    validate_ready_pr(pr, repo=repo, number=number)
    if (
        summary.get("countsComplete") is not True
        or summary.get("countsAreLowerBounds") is not False
        or summary.get("unavailableComponents")
    ):
        raise PrHelperError("PR check evidence is incomplete", repo=repo, pr=number)
    pending_client = sum(
        check.get("name") == "launchplane/owner-review"
        and check.get("status") != "completed"
        for check in checks["checkRuns"]
    ) if args.waiting_for_client else 0
    if (
        summary["failingCount"]
        or summary["pendingCount"] > pending_client
        or summary["unfinishedWorkflowRunCount"]
    ):
        raise PrHelperError("PR checks are pending or failing", repo=repo, pr=number, summary=summary)
    if not (summary["checkRunCount"] or summary["statusCount"]) and not args.allow_no_checks:
        raise PrHelperError("No checks exist for this head yet; wait for CI, or use --allow-no-checks for a repository with no CI checks configured", repo=repo, pr=number)
    # Bypass coalesced view caches: renewed holds and Client notes must be fresh.
    current = cmd_view(target, cache_enabled=False, include_body=True)["pr"]
    validate_ready_pr(current, repo=repo, number=number)
    if current["headRefOid"] != checks["headSha"] or current["labels"] != pr["labels"]:
        raise PrHelperError("PR head or labels changed during readiness checks; read it again", repo=repo, pr=number)
    client_refusal = client_handoff(
        current, checks["checkRuns"], repository=repo, waiting_for_client=args.waiting_for_client,
    ).refusal(accepted=args.waiting_for_client or any(
        check.get("name") == "launchplane/owner-review"
        and check.get("status") == "completed"
        and check.get("conclusion") == "success"
        for check in checks["checkRuns"]
    ))
    if client_refusal:
        raise PrHelperError(client_refusal, repo=repo, pr=number)
    review_gap = ready_review_gap(repo, number, current)
    if review_gap:
        raise PrHelperError(review_gap, repo=repo, pr=number)
    gaps = linked_issue_gaps(repo, number, current)
    if gaps:
        raise PrHelperError(linked_issue_gaps_message(gaps), repo=repo, pr=number, unownedIssues=list(gaps))
    try:
        body = handoff_body(checks["headSha"], waiting_for_client=args.waiting_for_client)
    except ValueError as exc:
        raise PrHelperError(str(exc), repo=repo, pr=number) from exc
    result = shared_comment("pr", repo, number, body, operation=CURRENT_OPERATION)
    return {**result, "headSha": checks["headSha"], "body": body, "summary": summary}


def ready_review_gap(repo: str, number: int, pr: dict[str, Any]) -> str:
    """Refuse a handoff whose required review of record has no recorded outcome."""
    reader, config_reader = (
        github_read_core.GitHubReader(
            gh_cmd=GH, expected_actor=EXPECTED_ACTOR, operation=read_operation(), cache_enabled=False,
        )
        for _ in range(2)
    )
    trusted = {login.casefold() for login in (EXPECTED_ACTOR, (pr.get("user") or {}).get("login")) if login}
    try:
        comments = reader.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="review_records")
        return review_evidence_gap(
            repo, number, base=pr["baseRefName"], head=pr["headRefOid"], comments=comments, trusted=trusted,
            # A repository without the config file answers 404; keep that expected
            # read out of the successful handoff's retry guidance.
            read_json=lambda path: (config_reader if "/contents/" in path else reader).get_json(path, step="review_records"),
            read_pages=lambda path: reader.paged_json(path, step_prefix="review_records"),
            missing=lambda error: isinstance(error, github_read_core.GitHubReadError) and error.result.status == 404,
        )
    except github_read_core.GitHubReadError as exc:
        raise PrHelperError(str(exc), failure=exc.result.failure, api_result=exc.result.as_dict(), diagnostics=exc.diagnostics, repo=repo, pr=number) from exc
    except (github_read_core.GitHubReadShapeError, KeyError, TypeError, ValueError) as exc:
        raise PrHelperError(f"Review-of-record evidence is unavailable: {exc}", diagnostics=reader.diagnostics(), repo=repo, pr=number) from exc
    finally:
        record_retry_summary(reader.retry_summary())
        if not any(result.status == 404 for result in config_reader.failed_results):
            record_retry_summary(config_reader.retry_summary())


def linked_issue_gaps(repo: str, number: int, pr: dict[str, Any]) -> dict[str, str]:
    owner = repo.split("/")[0].casefold()
    linked = [ref for ref in github_plan_status.references.linked_issues(repo, pr)
              if ref[0].split("/")[0].casefold() == owner]
    if not linked:
        return {}
    reader = github_read_core.GitHubReader(
        gh_cmd=GH, expected_actor=EXPECTED_ACTOR, operation=read_operation(), cache_enabled=False,
    )
    try:
        _, closing = github_plan_landing.closing_references(reader, repo, number, landed=False)
        closed_by_pr = {(issue_repo.casefold(), issue_number) for issue_repo, issue_number in closing}
        gaps = {}
        for issue_repo, issue_number in dict.fromkeys(linked):
            if (issue_repo.casefold(), issue_number) in closed_by_pr:
                continue
            if gap := remaining_steps_gap(reader, issue_repo, issue_number):
                gaps[f"{issue_repo}#{issue_number}"] = gap
        return gaps
    except github_read_core.GitHubReadError as exc:
        raise PrHelperError(f"Linked-issue follow-up evidence is unavailable: {exc}", failure=exc.result.failure,
                            api_result=exc.result.as_dict(), diagnostics=exc.diagnostics, repo=repo, pr=number) from exc
    except (github_read_core.GitHubReadShapeError, KeyError, TypeError, ValueError) as exc:
        raise PrHelperError(f"Linked-issue follow-up evidence is unavailable: {exc}", diagnostics=reader.diagnostics(), repo=repo, pr=number) from exc
    finally:
        record_retry_summary(reader.retry_summary())


def remaining_steps_gap(reader: github_read_core.GitHubReader, repo: str, number: int) -> str | None:
    issue = reader.get_json(f"/repos/{repo}/issues/{number}", step="linked_issue")
    if issue.get("state") != "open" or "pull_request" in issue:
        return None
    sub_issues = reader.paged_json(f"/repos/{repo}/issues/{number}/sub_issues", step_prefix="linked_issue_sub_issues")
    if any(child.get("state") == "open" for child in sub_issues):
        return None
    comments = reader.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="linked_issue_comments")
    trusted = github_plan_wait.authors(repo, expected_actor=EXPECTED_ACTOR)
    wait = github_plan_wait.current(issue, comments, trusted)
    if wait["state"] != "waiting":
        return NO_OWNER
    return director_question_gate.director_question_gap(
        repo, wait, read_comment=lambda path: readable_comment(reader, path),
        read_pages=lambda path: reader.paged_json(path, step_prefix="director_question"))


def readable_comment(reader: github_read_core.GitHubReader, path: str) -> dict[str, Any] | None:
    try:
        return reader.get_json(path, step="director_question")
    except github_read_core.GitHubReadError as exc:
        if exc.result.failure is not None and exc.result.failure.cause == "cancelled":
            raise
        return None


def linked_issue_gaps_message(gaps: dict[str, str]) -> str:
    unowned = [issue for issue, gap in gaps.items() if gap == NO_OWNER]
    steps = "; ".join(
        f"{issue}: write Closes {issue} if this PR finishes it, otherwise split every remaining step into an open "
        f"sub-issue that names its next actor (gh-plan.py create, then gh-plan.py --repo {issue.replace('#', ' link ')} subissue NEW) "
        "or record the event it waits on (gh-plan.py wait)"
        for issue in unowned)
    unasked = " ".join(f"{issue}: {gap}" for issue, gap in gaps.items() if gap != NO_OWNER)
    no_owner = f"PR leaves linked issues open with no owner for their remaining steps. {steps}. " if unowned else ""
    return f"{no_owner}{unasked}{' ' if unasked else ''}Then post ready again."


def validate_ready_pr(pr: dict[str, Any], *, repo: str, number: int) -> None:
    if pr.get("state") != "open" or pr.get("draft") is not False:
        raise PrHelperError("Ready handoff requires an open, non-draft PR", repo=repo, pr=number)
    if any(label["name"].casefold() == "merge-blocked" for label in pr["labels"]):
        raise PrHelperError("PR has merge-blocked set", repo=repo, pr=number)
    # Only a provider-computed conflict refuses; null or UNKNOWN mergeability is not one.
    merge_state = str(pr.get("mergeable_state") or pr.get("mergeStateStatus") or "").casefold()
    if pr.get("mergeable") in (False, "CONFLICTING") or merge_state == "dirty":
        raise PrHelperError(
            "PR conflicts with its base; refresh it under babysit-pr Conflicts, then post ready for the new head",
            repo=repo, pr=number, mergeable=pr.get("mergeable"), mergeStateStatus=merge_state.upper() or None,
        )


def cmd_checks(args: argparse.Namespace) -> dict[str, Any]:
    repo, number = resolve_pr(args.repo, args.pr)
    reader = github_read_core.GitHubReader(
        gh_cmd=GH,
        expected_actor=EXPECTED_ACTOR,
        operation=read_operation(),
    )
    try:
        payload = github_read_core.pull_request_checks(reader, repo, number)
    except github_read_core.GitHubReadError as exc:
        raise PrHelperError(
            str(exc),
            failure=exc.result.failure,
            api_result=exc.result.as_dict(),
            diagnostics=exc.diagnostics,
            repo=repo,
            pr=number,
        ) from exc
    except github_read_core.GitHubReadShapeError as exc:
        last_result = reader.last_result
        raise PrHelperError(
            str(exc),
            api_result=last_result.as_dict() if last_result else None,
            diagnostics=reader.diagnostics(),
            repo=repo,
            pr=number,
        ) from exc
    finally:
        record_retry_summary(reader.retry_summary())
    last_result = reader.last_result
    result_payload = {
        **payload,
        "ok": True,
        "actor": reader.actor,
        "expected_actor": reader.expected_actor,
        "completed_steps": reader.completed_steps,
        "diagnostics": reader.diagnostics(),
        **merge_observations(
            payload.get("pr"),
            readiness={
                "state": (
                    "reads_complete" if payload["summary"].get("countsComplete") is True
                    and payload["summary"].get("countsAreLowerBounds") is False
                    and not payload["summary"].get("unavailableComponents")
                    else "reads_degraded"
                ),
                "head_sha": payload.get("headSha"),
                "unavailable_components": payload["summary"].get("unavailableComponents") or [],
            },
        ),
    }
    if reader.diagnostics()["degraded"]:
        failed_result = reader.failed_results[0] if reader.failed_results else None
        failure = failed_result.failure if failed_result else github_api_core.FailureDetail(
            cause="degraded_read",
            message="PR check evidence is incomplete",
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            completed_steps=list(reader.completed_steps),
            failed_step=(reader.diagnostics().get("degradedComponents") or ["checks"])[0],
        )
        raise PrHelperError(
            "PR check evidence is incomplete",
            failure=failure,
            api_result=(failed_result or last_result).as_dict() if failed_result or last_result else None,
            **result_payload,
        )
    return result_payload


def update_branch_refusal(
    repo: str, number: int, mergeable_state: Any, required_by: Optional[str],
) -> Optional[github_api_core.FailureDetail]:
    """Refuse a base update that would only rerun CI.

    GitHub reports "behind" only when the base requires up-to-date branches.
    Any other refresh needs a named requirement; the merge train owns freshness.
    """
    if mergeable_state == "dirty":
        cause, message = "refresh_conflicting_branch", (
            "The PR conflicts with its base, which update-branch cannot resolve; merge the base locally "
            "with git-commit-as-bot --worktree /absolute/task/path --git-command merge, resolve, --continue and push "
            "(repo-workflow.md Merge Readiness)")
    elif mergeable_state == "behind" or required_by:
        return None
    elif mergeable_state in (None, "unknown"):
        cause, message = "mergeability_unknown", (
            f"GitHub has not computed mergeability yet; read it again with gh-pr.py --repo {repo} view {number}")
    else:
        cause, message = "refresh_not_required", (
            f"The base does not require this PR to be up to date (merge state {mergeable_state}), so an update "
            "would only rerun CI; the merge train owns freshness. Update only for a check or rule that requires "
            f"it: gh-pr.py --repo {repo} update-branch {number} --required-by \"<that check or rule>\"")
    return github_api_core.FailureDetail(
        cause=cause, message=message, retryable=False, fallback_eligible=False,
        disposition="stop", write_outcome="not_started", failed_step="update_branch_preflight",
    )


def cmd_update_branch(args: argparse.Namespace) -> dict[str, Any]:
    repo, number = resolve_pr(args.repo, args.pr)
    path = f"/repos/{repo}/pulls/{number}"
    pr = rest_json("GET", path)
    expected_head = str((pr.get("head") or {}).get("sha") or "")
    if pr.get("state") != "open" or not FULL_SHA_PATTERN.fullmatch(expected_head):
        raise HelperError("Updating a branch requires an open PR with a full head SHA")
    refusal = update_branch_refusal(repo, number, pr.get("mergeable_state"), args.required_by)
    if refusal:
        raise PrHelperError(
            refusal.message, failure=refusal, operation="update-branch", repo=repo, pr=number,
            headSha=expected_head, mergeableState=pr.get("mergeable_state"),
        )
    # A 202 accepts asynchronous work. Never replay an uncertain update: a
    # changed head alone cannot attribute a concurrent push to this request.
    accepted = rest_result("PUT", f"{path}/update-branch", {"expected_head_sha": expected_head})
    deadline = time.monotonic() + args.wait_seconds
    while True:
        try:
            refreshed = rest_json("GET", path)
        except PrHelperError as exc:
            # Preserve aggregate attempt evidence, but do not let a read's
            # retry fields describe replay of the accepted write.
            CURRENT_RETRY_FIELDS.update({
                "retry_eligible": False, "outcome_certainty": "unknown",
                "recommended_next_action": "observe_branch_update",
            })
            raise PrHelperError(
                "Branch update accepted but its new head could not be read; use view, do not repeat the update",
                failure=github_api_core.FailureDetail(
                    cause="update_observation_failed",
                    message="Update accepted; use view to observe completion, do not repeat update-branch",
                    retryable=False, fallback_eligible=False, disposition="stop",
                    write_outcome="unknown", completed_steps=["update_branch_accepted"],
                    failed_step="observe_updated_head",
                ),
                api_result={
                    "actor": accepted.actor, "expected_actor": accepted.expected_actor,
                    "status": accepted.status,
                },
                observation_error=exc.payload.get("api_result"),
                repo=repo, pr=number, updateAccepted=True, previousHeadSha=expected_head,
            ) from exc
        observed_head = str((refreshed.get("head") or {}).get("sha") or "")
        changed = FULL_SHA_PATTERN.fullmatch(observed_head) and observed_head != expected_head
        if changed or refreshed.get("state") != "open" or time.monotonic() >= deadline:
            return {
                "ok": True, "repo": repo, "pr": normalize_pr(refreshed),
                "actor": accepted.actor, "expected_actor": accepted.expected_actor,
                "completed_steps": ["update_branch_accepted", "read_head"],
                "update": {
                    "state": "head_changed" if changed else "accepted_unconfirmed",
                    "accepted": True, "previousHeadSha": expected_head, "requiredBy": args.required_by,
                    "newHeadSha": observed_head if changed else None,
                    "next_action": "Wait for checks on the observed head" if changed else
                    "Use gh-pr.py view to observe the head; do not repeat update-branch",
                },
            }
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def cmd_merge(args: argparse.Namespace) -> dict[str, Any]:
    repo, number = resolve_pr(args.repo, args.pr)
    pr: dict[str, Any] = rest_json("GET", f"/repos/{repo}/pulls/{number}")
    if pr.get("mergeable_state") == "behind":
        # GitHub reports "behind" only when the base requires up-to-date branches,
        # so the merge would be refused; send the caller to the update flow instead.
        raise PrHelperError(
            "PR head is behind a base that requires up-to-date branches",
            failure=github_api_core.FailureDetail(
                cause="update_behind_branch",
                message=f"First verify branch ownership and update authorization under repo-workflow.md Merge Readiness; then run gh-pr.py --repo {repo} update-branch {number}, wait for required checks on the new head, and follow the repository landing flow.",
                retryable=False,
                fallback_eligible=False,
                disposition="stop",
                write_outcome="not_started",
                failed_step="merge_preflight",
            ),
            operation="merge",
            repo=repo,
            pr=number,
            headSha=pr["head"]["sha"],
            baseSha=(pr.get("base") or {}).get("sha"),
            **merge_observations(pr),
        )
    payload: dict[str, Any] = {"merge_method": args.method, "sha": pr["head"]["sha"]}
    if args.commit_title:
        payload["commit_title"] = args.commit_title
    if args.commit_message:
        payload["commit_message"] = args.commit_message
    merge_path = f"/repos/{repo}/pulls/{number}/merge"
    expected_head_sha = str(pr["head"]["sha"])

    def reconcile_merge(
        failed_result: github_api_core.ApiResult,
        retry_context: github_api_core.ReconciliationContext,
    ) -> github_api_core.ReconciliationDecision:
        reconciliation_actor = failed_result.actor or failed_result.expected_actor
        if reconciliation_actor is None:
            return github_api_core.ReconciliationDecision(
                "failed",
                details={"failure": {"cause": "actor_unknown"}, "head_sha": expected_head_sha},
            )
        probe = github_api_core.call_gh_with_retry(
            "GET",
            f"/repos/{repo}/pulls/{number}",
            gh_cmd=GH,
            operation=CURRENT_OPERATION,
            actor=reconciliation_actor,
            expected_actor=reconciliation_actor,
            bucket="rest_core",
            is_write=False,
            retry_policy=retry_context.retry_policy,
            retry_runtime=retry_context.retry_runtime,
            deadline_at=retry_context.deadline_at,
        )
        record_retry_fields(probe)
        if not probe.ok or not isinstance(probe.body, dict):
            return github_api_core.ReconciliationDecision(
                "failed",
                details={"head_sha": expected_head_sha, "failure": probe.as_dict()},
            )
        refreshed = probe.body
        refreshed_head = str((refreshed.get("head") or {}).get("sha") or "")
        merged = bool(refreshed.get("merged")) or bool(refreshed.get("merged_at"))
        observed_merge_sha = str(refreshed.get("merge_commit_sha") or "")
        details = {
            "head_sha": expected_head_sha,
            "observed_head_sha": refreshed_head or None,
            "merged": merged,
            "merge_commit_sha": observed_merge_sha or None,
            "state": refreshed.get("state"),
        }
        if not refreshed_head or refreshed_head.casefold() != expected_head_sha.casefold():
            return github_api_core.ReconciliationDecision("ambiguous", details=details)
        if merged:
            if not FULL_SHA_PATTERN.fullmatch(observed_merge_sha):
                return github_api_core.ReconciliationDecision("failed", details=details)
            return github_api_core.ReconciliationDecision(
                "matched",
                body={"merged": True, "sha": observed_merge_sha, "message": "Merge confirmed by PR re-read"},
                details=details,
            )
        if refreshed.get("state") == "open":
            return github_api_core.ReconciliationDecision("no_match", details=details)
        return github_api_core.ReconciliationDecision("failed", details=details)

    try:
        result = rest_result("PUT", merge_path, payload, reconcile=reconcile_merge).body
    except HelperError as exc:
        api_result = exc.payload.get("api_result") if isinstance(exc, PrHelperError) else None
        merge_failure = exc.failure if isinstance(exc, PrHelperError) else None
        if merge_failure and merge_failure.cause == "permission_denied":
            capability = {
                "observed_outcome": "denied",
                "authority": "denied_for_operation",
                "cause": merge_failure.cause,
            }
        else:
            capability = {
                "observed_outcome": "rejected" if merge_failure and merge_failure.write_outcome == "rejected" else "unknown",
                "authority": "unknown",
                "cause": merge_failure.cause if merge_failure else None,
            }
        raise PrHelperError(
            "PR merge failed",
            failure=merge_failure,
            detail=str(exc),
            operation="merge",
            repo=repo,
            pr=number,
            endpoint=merge_path,
            method=args.method,
            headSha=pr["head"]["sha"],
            hint=merge_failure_hint(str(exc), cause=merge_failure.cause if merge_failure else None),
            api_result=api_result,
            **merge_observations(pr, capability=capability),
        ) from exc
    if not result.get("merged"):
        message = str(result.get("message") or "GitHub did not merge the pull request")
        raise PrHelperError(
            "PR merge was not completed",
            failure=github_api_core.FailureDetail(
                cause="merge_rejected",
                message=message,
                retryable=False,
                fallback_eligible=False,
                disposition="stop",
                write_outcome="rejected",
                failed_step="merge_pull_request",
            ),
            operation="merge",
            repo=repo,
            pr=number,
            merge=result,
            **merge_observations(
                pr,
                capability={"observed_outcome": "rejected", "authority": "unknown", "cause": "merge_rejected"},
            ),
        )
    merge_sha = str(result.get("sha") or "")
    if not FULL_SHA_PATTERN.fullmatch(merge_sha):
        refreshed_pr = rest_json("GET", f"/repos/{repo}/pulls/{number}")
        refreshed_sha = str(refreshed_pr.get("merge_commit_sha") or "")
        refreshed_merged = bool(refreshed_pr.get("merged")) or bool(refreshed_pr.get("merged_at"))
        refreshed_head_sha = str((refreshed_pr.get("head") or {}).get("sha") or "")
        if (
            not refreshed_merged
            or not FULL_SHA_PATTERN.fullmatch(refreshed_sha)
            or not refreshed_head_sha
            or refreshed_head_sha.casefold() != expected_head_sha.casefold()
        ):
            raise PrHelperError(
                "PR merge succeeded without a trustworthy final merge commit SHA",
                failure=github_api_core.FailureDetail(
                    cause="merge_identity_unavailable",
                    message="Re-read the merged PR before any post-merge reconciliation; do not retry the merge.",
                    retryable=False,
                    fallback_eligible=False,
                    disposition="stop",
                    write_outcome="unknown",
                    completed_steps=["merge_pull_request"],
                    failed_step="confirm_merge_commit",
                ),
                operation="merge",
                repo=repo,
                pr=number,
                merge=result,
                refreshedPr=normalize_pr(refreshed_pr),
                **merge_observations(
                    pr,
                    capability={
                        "observed_outcome": "accepted_unverified",
                        "authority": "confirmed_for_operation",
                    },
                ),
            )
        pr = refreshed_pr
        merge_sha = refreshed_sha
        result["sha"] = merge_sha
    pr = {
        **pr,
        "state": "closed",
        "merged": True,
        "merge_commit_sha": merge_sha,
    }
    deleted = None
    if args.delete_branch and result.get("merged"):
        head_repo = pr.get("head", {}).get("repo") or {}
        head_full_name = head_repo.get("full_name")
        head_ref = pr.get("head", {}).get("ref")
        base_ref = pr.get("base", {}).get("ref")
        if head_full_name == repo and head_ref and head_ref != base_ref:
            deleted = delete_ref(repo, f"heads/{head_ref}")
    return {
        "ok": True,
        "repo": repo,
        "pr": normalize_pr(pr),
        **merge_observations(
            pr,
            capability={
                "observed_outcome": "merged",
                "authority": (
                    "not_attributed_to_actor" if result.get("message") == "Merge confirmed by PR re-read"
                    else "confirmed_for_operation"
                ),
            },
        ),
        "merge": result,
        "mergeCommitOid": merge_sha,
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

    winner_ref = pr_reference(repo, winner_repo, winner_number, winner)
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
    + r")\s+(https://github[.]com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d+))(?:\b|(?=[?#]))",
    re.IGNORECASE,
)


def neutralize_issue_closing_keywords(body: str) -> tuple[str, list[dict[str, str]]]:
    replacements: list[dict[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        original = match.group()
        replacement = f"Refs {match.group(2)}"
        replacements.append({"from": original, "to": replacement})
        return replacement

    def replace_url(match: re.Match[str]) -> str:
        original = match.group()
        replacement = f"Refs {match.group(3)}#{match.group(4)}"
        replacements.append({"from": original, "to": replacement})
        return replacement

    body = CLOSING_ISSUE_URL_RE.sub(replace_url, body)
    return CLOSING_REFERENCE_RE.sub(replace, body), replacements


def pr_reference(current_repo: str, winner_repo: str, winner_number: int, winner: dict[str, Any]) -> str:
    """Format the owner-facing `[repo#N](URL) title` reference to the winning PR."""
    owner, name = winner_repo.split("/", 1)
    label = f"{name}#{winner_number}" if owner == current_repo.split("/", 1)[0] else f"{winner_repo}#{winner_number}"
    url = winner.get("html_url") or f"https://github.com/{winner_repo}/pull/{winner_number}"
    title = " ".join(str(winner.get("title") or "").split())
    return f"[{label}]({url}) {title}".rstrip()


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


def merge_observations(
    pr: Optional[dict[str, Any]],
    *,
    readiness: Optional[dict[str, Any]] = None,
    capability: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    item = pr or {}
    state = item.get("mergeable_state") or item.get("mergeStateStatus")
    normalized_state = MERGE_STATE_STATUS.get(str(state), str(state).upper()) if state else None
    return {
        "readiness_evidence": readiness or {"state": "not_read"},
        "mergeability": {
            "state": normalized_state or "UNKNOWN",
            "mergeable": item.get("mergeable"),
            "source": "pull_request_metadata" if pr else "not_read",
        },
        "observed_merge_capability": capability or {
            "observed_outcome": "not_attempted", "authority": "unknown",
        },
    }


def merge_failure_hint(message: str, *, cause: Optional[str] = None) -> str:
    if cause == "unresolved_review_threads":
        return "Resolve the unresolved PR review threads before attempting the merge again."
    if cause == "required_status_checks_expected":
        return (
            "Wait for GitHub to receive the required status checks for this head, "
            "rerun gh-pr.py checks, then retry the merge."
        )
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
    normalized = {
        "number": pr.get("number"),
        "title": pr.get("title"),
        "state": pr.get("state"),
        "draft": pr.get("draft"),
        "isDraft": pr.get("draft"),
        "merged": merged,
        "mergedAt": pr.get("merged_at"),
        "mergeCommitOid": pr.get("merge_commit_sha"),
        "mergeable": pr.get("mergeable"),
        "mergeable_state": mergeable_state,
        "mergeStateStatus": MERGE_STATE_STATUS.get(str(mergeable_state), str(mergeable_state).upper()) if mergeable_state else None,
        "statusCheckRollup": pr.get("statusCheckRollup"),
        "labels": labels,
        "url": pr.get("html_url"),
        "baseRefName": (pr.get("base") or {}).get("ref"),
        "headRefName": (pr.get("head") or {}).get("ref"),
        "headRefOid": (pr.get("head") or {}).get("sha"),
        "headRepository": ((pr.get("head") or {}).get("repo") or {}).get("full_name"),
        "baseRepository": ((pr.get("base") or {}).get("repo") or {}).get("full_name"),
    }
    if pr.get("reviewDecision") is not None:
        normalized["reviewDecision"] = pr.get("reviewDecision")
    return normalized


def resolve_repo(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    remote = run_git(["remote", "get-url", "origin"])
    match = re.search(r"github[.]com[:/]([^/]+)/([^/.]+)(?:[.]git)?$", remote.strip())
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
    return resolve_pr_target(repo, value)


def parse_pr_url(value: Optional[str]):
    if not value:
        return None
    match = re.search(r"^(?:https?://|git@)?[^/:]+[:/]([^/]+)/([^/]+)/(?:pull|pulls)/(\d+)(?:[/?#].*)?$", value)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}", int(match.group(3))


def resolve_pr_target(repo: str, value: Optional[str]) -> Tuple[str, int]:
    if value:
        if value.isdigit():
            return repo, int(value)
        branch = value
        missing_message = f"No open PR found for branch {branch}; pass a PR number"
    else:
        branch = run_git(["branch", "--show-current"]).strip()
        if not branch:
            raise HelperError("Current branch is detached; pass a PR number")
        missing_message = f"No open PR found for current branch {branch}; pass a PR number"

    try:
        origin_repo = github_read_core.resolve_repo_from_remote(pathlib.Path.cwd(), "origin")
    except ValueError:
        origin_repo = repo
    candidate_repos = [repo]
    try:
        upstream_repo = github_read_core.resolve_repo_from_remote(pathlib.Path.cwd(), "upstream")
    except ValueError:
        upstream_repo = None
    if upstream_repo and upstream_repo not in candidate_repos:
        candidate_repos.append(upstream_repo)

    head = f"{origin_repo.split('/', 1)[0]}:{branch}"
    for candidate_repo in candidate_repos:
        candidate_pulls = rest_json(
            "GET",
            f"/repos/{candidate_repo}/pulls",
            params={"head": head, "state": "open"},
        )
        if isinstance(candidate_pulls, list):
            pulls = [item for item in candidate_pulls if isinstance(item, dict)]
            if len(pulls) == 1:
                return candidate_repo, int(pulls[0]["number"])
            if len(pulls) > 1:
                raise HelperError(f"Multiple open PRs found for branch {branch}; pass a PR number")
    raise HelperError(missing_message)


def rest_json(
    method: str,
    path: str,
    payload: Optional[dict] = None,
    *,
    params: Optional[dict] = None,
    reconcile: Optional[
        Callable[
            [github_api_core.ApiResult, github_api_core.ReconciliationContext],
            github_api_core.ReconciliationDecision,
        ]
    ] = None,
) -> Any:
    return rest_result(method, path, payload, params=params, reconcile=reconcile).body


def rest_result(
    method: str,
    path: str,
    payload: Optional[dict] = None,
    *,
    params: Optional[dict] = None,
    reconcile: Optional[
        Callable[
            [github_api_core.ApiResult, github_api_core.ReconciliationContext],
            github_api_core.ReconciliationDecision,
        ]
    ] = None,
) -> github_api_core.ApiResult:
    if params:
        path = path_with_query(path, params)
    result = github_api_core.call_gh_with_retry(
        method,
        path,
        payload,
        gh_cmd=GH,
        operation=read_operation(),
        expected_actor=EXPECTED_ACTOR,
        bucket="rest_core",
        reconcile=reconcile,
    )
    record_retry_fields(result)
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


def rest_page_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("check_runs", "statuses", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def delete_ref(repo: str, ref: str) -> dict[str, Any]:
    operation = "github.pr.delete_ref"
    delete_path = f"/repos/{repo}/git/refs/{ref}"

    def reconcile_delete(
        failed_result: github_api_core.ApiResult,
        retry_context: github_api_core.ReconciliationContext,
    ) -> github_api_core.ReconciliationDecision:
        reconciliation_actor = failed_result.actor or failed_result.expected_actor
        if reconciliation_actor is None:
            return github_api_core.ReconciliationDecision(
                "failed",
                details={"ref": ref, "failure": {"cause": "actor_unknown"}},
            )
        probe = github_api_core.call_gh_with_retry(
            "GET",
            f"/repos/{repo}/git/ref/{ref}",
            gh_cmd=GH,
            operation=operation,
            actor=reconciliation_actor,
            expected_actor=reconciliation_actor,
            bucket="rest_core",
            is_write=False,
            retry_policy=retry_context.retry_policy,
            retry_runtime=retry_context.retry_runtime,
            deadline_at=retry_context.deadline_at,
        )
        record_retry_fields(probe)
        if probe.ok:
            return github_api_core.ReconciliationDecision(
                "no_match",
                details={"ref": ref, "state": "present"},
            )
        if probe.failure and probe.failure.cause == "not_found":
            return github_api_core.ReconciliationDecision(
                "matched",
                body={"ref": ref, "deleted": True},
                details={"ref": ref, "state": "absent"},
            )
        return github_api_core.ReconciliationDecision(
            "failed",
            details={"ref": ref, "failure": probe.as_dict()},
        )

    result = github_api_core.call_gh_with_retry(
        "DELETE",
        delete_path,
        gh_cmd=GH,
        operation=operation,
        expected_actor=EXPECTED_ACTOR,
        bucket="rest_core",
        reconcile=reconcile_delete,
    )
    already_absent = bool(not result.ok and result.failure and result.failure.cause == "not_found")
    if not result.ok and result.status == 422 and not already_absent:
        # GitHub answers a delete of a missing ref with 422, not 404, for example after its own
        # auto-delete of a merged branch. Confirm by reading the ref rather than trusting the message.
        ref_check = github_api_core.call_gh_with_retry(
            "GET",
            f"/repos/{repo}/git/ref/{ref}",
            gh_cmd=GH,
            operation=operation,
            expected_actor=EXPECTED_ACTOR,
            bucket="rest_core",
            is_write=False,
        )
        already_absent = bool(not ref_check.ok and ref_check.failure and ref_check.failure.cause == "not_found")
    if already_absent:
        result.ok = True
        result.status = 204
        result.body = {"ref": ref, "deleted": True}
        result.failure = None
        result.failed_step = None
        if result.retry_summary is not None:
            result.retry_summary.outcome_certainty = "reconciled_applied"
            result.retry_summary.reconciliation = {
                "strategy": "read_ref_after_delete",
                "result": "matched",
                "ref": ref,
                "state": "absent",
            }
            result.retry_summary.recommended_next_action = "none"
            result.retry_summary.exhausted_reason = None
    deleted: dict[str, Any] = {"ref": ref, "deleted": result.ok, "already_absent": already_absent, "stderr": ""}
    if not result.ok:
        deleted["stderr"] = result.failure.message if result.failure else "GitHub API request failed"
        deleted["api_result"] = result.as_dict()
    record_retry_fields(result)
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


def read_text_file(path: str, *, preserve_newlines: bool = False, **payload: Any) -> str:
    try:
        if path == "-":
            if preserve_newlines:
                return sys.stdin.buffer.read().decode()
            return sys.stdin.read()
        newline = "" if preserve_newlines else None
        with pathlib.Path(path).open(encoding="utf-8", newline=newline) as stream:
            return stream.read()
    except OSError as exc:
        raise PrHelperError("Could not read body file", detail=str(exc), path=path, **payload) from exc


def run_pr_write(args: list[str], **payload: Any) -> subprocess.CompletedProcess[str]:
    operation = f"github.pr.{str(payload.get('operation') or 'write').replace('-', '_')}"
    proc, result = run_gh_with_retry(args, operation=operation, is_write=True)
    if not result.ok:
        if (
            operation == "github.pr.create" and result.failure is not None
            and result.failure.cause == "validation_error"
            and result.failure.write_outcome == "not_started"
            and re.fullmatch(r"could not add label: '[^'\r\n]+' not found", result.failure.message)
        ):
            result.failure.message += (
                "; PR creation was not started. Repair the requested label only within existing "
                "authorization, then read back the label and all-state PRs for the same head/base. "
                "If no matching PR exists and the rejected preflight is verified, issue a new "
                "gh-pr.py create; do not replay automatically."
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
    return match.group() if match else None


def gh_json(args: list[str]) -> Any:
    proc, result = run_gh_with_retry(args, operation=CURRENT_OPERATION, is_write=False)
    if not result.ok:
        message = (proc.stderr or proc.stdout or "gh command failed").strip()
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


def run_gh_with_retry(
    args: list[str],
    *,
    operation: str,
    is_write: bool,
) -> tuple[subprocess.CompletedProcess[str], github_api_core.ApiResult]:
    last_proc: subprocess.CompletedProcess[str] | None = None
    observed_bucket = "mixed"
    retry_rule, _ = github_api_core.operation_retry_rule(operation)
    probe_allowed = bool(
        retry_rule and retry_rule.retry_eligibility in {"safe", "conditional"}
    )

    def attempt(timeout_seconds: Optional[float] = None) -> github_api_core.ApiResult:
        nonlocal last_proc, observed_bucket
        attempt_started = time.monotonic()
        try:
            proc = run_gh(args, timeout_seconds=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            partial_stderr = github_api_core.subprocess_output_text(exc.stderr).strip()
            timeout_message = "GitHub command exceeded the effective retry deadline"
            proc = subprocess.CompletedProcess(
                args=[GH, *args],
                returncode=124,
                stdout="",
                stderr="\n".join(part for part in (partial_stderr, timeout_message) if part),
            )
            last_proc = proc
            return github_api_core.subprocess_timeout_result(
                operation=operation,
                is_write=is_write,
                actor=EXPECTED_ACTOR,
                expected_actor=EXPECTED_ACTOR,
                host=github_api_core.DEFAULT_HOST,
                transport="gh_cli_wrapper",
                bucket=observed_bucket,
                stderr=exc.stderr,
            )
        last_proc = proc
        reported_actor = github_api_core.actor_from_gh_stderr(proc.stderr) or EXPECTED_ACTOR
        expected_context = (
            None if github_api_core.active_fallback_was_authorized(proc.stderr) else EXPECTED_ACTOR
        )
        if proc.returncode == 0:
            return github_api_core.ApiResult(
                ok=True,
                status=0,
                body=None,
                operation=operation,
                actor=reported_actor,
                expected_actor=expected_context,
                host=github_api_core.DEFAULT_HOST,
                transport="gh_cli_wrapper",
                bucket=observed_bucket,
            )
        attempt_result = github_api_core.legacy_process_result(
            proc.returncode,
            proc.stdout,
            proc.stderr,
            operation=operation,
            is_write=is_write,
            actor=reported_actor,
            expected_actor=expected_context,
            transport="gh_cli_wrapper",
            bucket=observed_bucket,
        )
        if (
            probe_allowed
            and attempt_result.failure is not None
            and attempt_result.failure.cause in github_api_core.PRIMARY_RATE_LIMIT_CAUSES
            and not attempt_result.failure.rate_limit
        ):
            probe_timeout = timeout_seconds
            if probe_timeout is not None:
                probe_timeout -= time.monotonic() - attempt_started
                if probe_timeout <= 0:
                    return attempt_result
            probe = github_api_core.rate_limit_probe(
                gh_cmd=GH,
                actor=reported_actor,
                expected_actor=expected_context,
                timeout_seconds=probe_timeout,
            )
            attempt_result = github_api_core.legacy_process_result(
                proc.returncode,
                proc.stdout,
                proc.stderr,
                operation=operation,
                is_write=is_write,
                actor=reported_actor,
                expected_actor=expected_context,
                transport="gh_cli_wrapper",
                bucket=observed_bucket,
                rate_limit_result=probe,
            )
        if attempt_result.bucket:
            observed_bucket = attempt_result.bucket
        return attempt_result

    result = github_api_core.run_with_retry(
        lambda: attempt(),
        operation=operation,
        is_write=is_write,
        actor=EXPECTED_ACTOR,
        expected_actor=EXPECTED_ACTOR,
        bucket="mixed",
        attempt_with_timeout=lambda timeout: attempt(timeout),
    )
    record_retry_fields(result)
    if last_proc is None:
        raise PrHelperError("gh command did not execute", api_result=result.as_dict(), command=args[:2])
    return last_proc, result


def run_gh(
    args: list[str],
    *,
    timeout_seconds: Optional[float] = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [GH, *args],
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
    )


def run_git(args: list[str]) -> str:
    proc = subprocess.run(["git", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise HelperError(proc.stderr.strip() or "git command failed")
    return proc.stdout


if __name__ == "__main__":
    raise SystemExit(main())
