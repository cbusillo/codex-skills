#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Refuse to close an issue while an outside person's comment is unanswered.

The existing comment radar (github_unanswered_comments.py) reads the issue and
the threads it links. A comment blocks closure until an owner or agent reply is
posted after its latest version; an edit to an older comment is not a reply. A
recorded Director decision on the issue is the only override.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from github_read import GitHubReader, GitHubReadError, GitHubReadShapeError

HELPER = Path(__file__).resolve().parents[2] / "github-work-rollup/scripts/github_unanswered_comments.py"
HELPER_TIMEOUT_SECONDS = 600
HELPER_CLEAR_OR_ATTENTION_EXITS = (0, 2)
HELPER_DEGRADED_EXIT = 3
DECISION_PREFIXES = ("Director decision:", "Owner decision:")
THREAD_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)(?![\w/])", re.IGNORECASE)
API_ISSUE_URL = re.compile(r"/repos/([\w.-]+/[\w.-]+)/issues/(\d+)$", re.IGNORECASE)
QUALIFIED_REF = re.compile(r"(?<![\w/.-])([\w.-]+/[\w.-]+)#(\d+)\b")
SHORT_REF = re.compile(r"(?<![\w/#&-])#(\d+)\b")
RELATIONSHIPS = ("blocked_by", "blocking", "sub_issues")


class CloseAttentionError(Exception):
    def __init__(self, message: str, *, cause: str, payload: dict[str, Any]) -> None:
        super().__init__(message)
        self.cause = cause
        self.payload = payload


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def thread_ref(repo: str, number: int) -> str:
    return f"{repo}#{number}"


def body_references(repo: str, body: str) -> list[tuple[str, int]]:
    refs = [(match[0], int(match[1])) for match in THREAD_URL.findall(body)]
    refs += [(match[0], int(match[1])) for match in QUALIFIED_REF.findall(body)]
    refs += [(repo, int(number)) for number in SHORT_REF.findall(body)]
    return refs


def same_owner_body_references(repo: str, issue: dict[str, Any]) -> list[tuple[str, int]]:
    owner = repo.split("/", 1)[0].casefold()
    return [ref for ref in body_references(repo, str(issue.get("body") or ""))
            if ref[0].split("/", 1)[0].casefold() == owner]


def missing_body_references(reader: GitHubReader, repo: str, issue: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for ref_repo, ref_number in dict.fromkeys(same_owner_body_references(repo, issue)):
        try:
            reader.get_json(f"repos/{ref_repo}/issues/{ref_number}", step="close_attention_reference")
        except GitHubReadError as error:
            if error.result.status == 404:
                missing.append(thread_ref(ref_repo, ref_number))
    return missing


def names_thread(gap: str, ref: str) -> bool:
    ref_repo, ref_number = ref.split("#")
    return re.search(rf"repos/{re.escape(ref_repo)}/issues/{ref_number}(?!\d)", gap, re.IGNORECASE) is not None


def describe_missing(repo: str, ref: str) -> str:
    ref_repo, ref_number = ref.split("#")
    if ref_repo.casefold() == repo.casefold():
        return f"- #{ref_number} does not exist in {repo}; write it as owner/repo#{ref_number} or a full link."
    return f"- {ref} does not exist; correct it to the intended owner/repo#{ref_number} or a full link."


def linked_threads(reader: GitHubReader, repo: str, number: int, issue: dict[str, Any]) -> list[str]:
    """The issue's native relationships, in any account, and same-owner references in its body."""
    native: list[tuple[str, int]] = []
    for relationship in RELATIONSHIPS:
        path = (f"repos/{repo}/issues/{number}/sub_issues" if relationship == "sub_issues"
                else f"repos/{repo}/issues/{number}/dependencies/{relationship}")
        for item in reader.paged_json(path, step_prefix=f"close_attention_{relationship}"):
            match = THREAD_URL.match(str(item.get("html_url") or ""))
            if match:
                native.append((match[1], int(match[2])))
    parent = API_ISSUE_URL.search(str(issue.get("parent_issue_url") or ""))
    if parent:
        native.append((parent[1], int(parent[2])))
    threads: list[str] = []
    seen = {(repo.casefold(), number)}
    for linked_repo, linked_number in native + same_owner_body_references(repo, issue):
        key = (linked_repo.casefold(), linked_number)
        if key in seen:
            continue
        seen.add(key)
        threads.append(thread_ref(linked_repo, linked_number))
    return threads


def run_radar(threads: list[str], gh_cmd: str | None = None,
              run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> dict[str, Any]:
    # The closing repository's owner is the Director on every thread, including linked threads in other accounts.
    # A close checks the threads as they are now, so nothing fetched falls outside a scan end.
    command = ["uv", "run", "--quiet", str(HELPER), "--format", "json", "--include-handled", "--no-cutoff",
               "--director-login", threads[0].split("/", 1)[0]]
    for thread in threads:
        command += ["--thread", thread]
    # The radar reads through the same gh route as the close it guards.
    env = {**os.environ, "GITHUB_UNANSWERED_COMMENTS_GH": gh_cmd} if gh_cmd else None
    try:
        result = run(command, capture_output=True, text=True, check=False, timeout=HELPER_TIMEOUT_SECONDS, env=env)
        payload = json.loads(result.stdout or "null")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        return {"status": "degraded", "error": f"comment check did not finish: {error}"}
    if isinstance(payload, dict) and result.returncode == HELPER_DEGRADED_EXIT and payload.get("coverage"):
        return {**payload, "status": "degraded"}
    if not isinstance(payload, dict) or result.returncode not in HELPER_CLEAR_OR_ATTENTION_EXITS:
        detail = payload.get("error") if isinstance(payload, dict) else None
        return {"status": "degraded",
                "error": str(detail or (result.stderr or "").strip()[-500:] or f"exit {result.returncode}")}
    return payload


def unanswered_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    # Only later_reply_url counts: the radar's targeted response follows edit time, so it is not used here.
    return [
        {key: row.get(key) for key in ("repo", "number", "author", "comment_url", "created_at",
                                        "updated_at", "body_excerpt")}
        for row in [*(payload.get("attention") or []), *(payload.get("handled") or [])]
        if isinstance(row, dict)
        and row.get("author_class") != "automation"
        and not row.get("later_reply_url")
    ]


def coverage_gaps(payload: dict[str, Any]) -> list[str]:
    if payload.get("status") != "degraded":
        return []
    errors = (payload.get("coverage") or {}).get("errors") or []
    gaps = [f"{error.get('repo', '?')} {error.get('lane', '?')}: {error.get('error', '?')}"
            for error in errors if isinstance(error, dict)]
    return gaps or [str(payload.get("error") or "coverage unknown")]


def verify_decision(reader: GitHubReader, repo: str, number: int, url: str,
                    rows: list[dict[str, Any]]) -> dict[str, Any]:
    """A Director comment, or a Director-approved decision record, on this issue after every blocking comment."""
    match = re.fullmatch(rf"https://github\.com/{re.escape(repo)}/issues/{number}#issuecomment-(\d+)",
                         url.strip(), re.IGNORECASE)
    if not match:
        raise ValueError(f"--unanswered-decision must link a comment on {thread_ref(repo, number)}")
    comment_id = int(match[1])
    comment = reader.get_json(f"repos/{repo}/issues/comments/{comment_id}", step="close_attention_decision")
    commented_on = API_ISSUE_URL.search(str(comment.get("issue_url") or ""))
    if not commented_on or (commented_on[1].casefold(), int(commented_on[2])) != (repo.casefold(), number):
        raise ValueError(f"{url} is not a comment on {thread_ref(repo, number)}")
    owner = repo.split("/", 1)[0].casefold()
    created = timestamp(comment.get("created_at"))
    newest = max((moment for row in rows for moment in (timestamp(row.get("created_at")),
                                                         timestamp(row.get("updated_at"))) if moment),
                 default=None)
    if created is None or (newest is not None and created <= newest):
        raise ValueError(f"{url} predates the newest unanswered comment")
    author = ((comment.get("user") or {}).get("login") or "").casefold()
    if author == owner:
        return {"approval": "director_comment", "comment_url": url}
    body = comment.get("body") or ""
    unedited = comment.get("updated_at") == comment.get("created_at")
    if unedited and body.splitlines()[:1] and body.splitlines()[0].startswith(DECISION_PREFIXES):
        reactions = reader.paged_json(f"repos/{repo}/issues/comments/{comment_id}/reactions",
                                      step_prefix="close_attention_decision_reactions")
        for reaction in reactions:
            login = ((reaction.get("user") or {}).get("login") or "").casefold()
            reacted = timestamp(reaction.get("created_at"))
            if reaction.get("content") == "+1" and login == owner and reacted is not None and reacted >= created:
                return {"approval": "director_reaction", "comment_url": url,
                        "reacted_at": reaction.get("created_at")}
    raise ValueError(f"{url} is neither the Director's comment nor an unedited Director decision record "
                     "the Director approved with a thumbs-up")


def describe(row: dict[str, Any]) -> str:
    excerpt = str(row.get("body_excerpt") or "").strip()
    return (f"- {row.get('author')} on {thread_ref(str(row.get('repo')), int(row.get('number') or 0))} "
            f"at {row.get('created_at')}: {row.get('comment_url')}" + (f" \"{excerpt}\"" if excerpt else ""))


def check(repo: str, number: int, *, issue: dict[str, Any] | None = None, decision_url: str | None = None,
          gh_cmd: str | None = None, reader: GitHubReader | None = None,
          radar: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return the passed check, or raise CloseAttentionError before any write."""
    if issue is not None and str(issue.get("state") or "").casefold() == "closed":
        return {"result": "skipped", "reason": "already_closed"}
    client = reader or (GitHubReader(operation="github.close_attention", repository=repo, gh_cmd=gh_cmd) if gh_cmd
                        else GitHubReader(operation="github.close_attention", repository=repo))
    try:
        issue = issue if issue is not None else client.get_json(f"repos/{repo}/issues/{number}",
                                                                step="close_attention_issue")
        if str(issue.get("state") or "").casefold() == "closed":
            return {"result": "skipped", "reason": "already_closed"}
        threads = [thread_ref(repo, number), *linked_threads(client, repo, number, issue)]
    except (GitHubReadError, GitHubReadShapeError, AttributeError, TypeError) as error:
        threads, payload = [thread_ref(repo, number)], {"status": "degraded",
                                                         "error": f"linked threads unreadable: {error}"}
    else:
        payload = (radar or run_radar)(threads, gh_cmd)
    rows, gaps = unanswered_rows(payload), coverage_gaps(payload)
    result = {"threads": threads, "unanswered": rows, "coverage_gaps": gaps}
    if not rows and not gaps:
        return {"result": "passed", **result}
    if decision_url:
        try:
            decision = verify_decision(client, repo, number, decision_url, rows)
        except (ValueError, GitHubReadError, GitHubReadShapeError, AttributeError, TypeError) as error:
            raise CloseAttentionError(f"Cannot close {thread_ref(repo, number)}: {error}",
                                      cause="decision_unverified", payload=result) from error
        return {"result": "director_decision", "decision": decision, **result}
    missing = missing_body_references(client, repo, issue) if gaps and isinstance(issue, dict) else []
    gaps = [gap for gap in gaps if not any(names_thread(gap, ref) for ref in missing)]
    lines = [f"Cannot close {thread_ref(repo, number)}:"]
    if missing:
        lines.append("its body cites issues that do not exist; correct each reference in the body, then rerun:")
        lines += [describe_missing(repo, ref) for ref in missing]
    if rows:
        lines.append("these comments from outside people have no reply since they were posted:")
        lines += [describe(row) for row in rows]
    if gaps:
        lines.append("comments could not be fully checked on these threads:")
        lines += [f"- {gap}" for gap in gaps]
    if rows or gaps:
        lines.append("Read and answer each comment with a new reply on its thread (editing an older comment does "
                     "not count), or rerun once the threads are readable. Only the Director can close over them: "
                     "rerun with --unanswered-decision linking the Director's comment on this issue, or a "
                     "\"Director decision:\" record the Director approved with a thumbs-up. Do not close it another "
                     "way.")
    cause = "unanswered_comments" if rows else "coverage_unknown" if gaps else "missing_references"
    raise CloseAttentionError("\n".join(lines), cause=cause, payload={**result, "missing_references": missing})
