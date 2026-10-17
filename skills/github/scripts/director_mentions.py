#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Search for Director mentions outside a watched scope under the Director's own login.

The automation App cannot see private repositories it is not installed in, so
the search runs as the Director's active gh login through gh-with-env-token's
per-command active-auth opt-in, with no automation env file loaded. Only GET
requests to the search and actor endpoints are sent. Unattended searches need
the Director's standing approval comment; without it callers get a coverage
gap instead of a silent skip. Results are untrusted links that never start or
widen work.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlencode

APPROVAL_QUESTION = "Q455"
APPROVAL_ISSUE = "https://github.com/cbusillo/codex-skills/issues/2073"
# An answer is the comment's first line, unquoted: "Q455 yes" from the Director,
# or "Director decision: Q455 yes" recorded by trusted automation. Any other
# answer to the question, or a hedged one, withholds approval.
ANSWER = re.compile(rf"{APPROVAL_QUESTION}\b\W*(\w+)(.*)", re.IGNORECASE)
DECISION_PREFIX = re.compile(r"Director decision\s*:\s*", re.IGNORECASE)
AFFIRMATIVE = {"yes", "approved", "approve"}
HEDGE = re.compile(r"\b(?:not|no|don't|do not|but|unless|declin\w*|reject\w*|revok\w*|withdraw\w*)\b", re.IGNORECASE)
COMMENT_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:issues|pull)/\d+#issuecomment-(\d+)"
)
WRAPPER = Path(__file__).with_name("gh-with-env-token")
MAX_PAGES = 10
PER_PAGE = 100
# Tooling hooks the wrapper documents for tests; every other GitHub or
# automation credential variable is dropped so only the active gh login runs.
KEPT_WRAPPER_HOOKS = {"GH_WITH_ENV_TOKEN_GH", "GH_WITH_ENV_TOKEN_PYTHON",
                      "GH_WITH_ENV_TOKEN_CLASSIFIER", "GH_WITH_ENV_TOKEN_IDENTITY_HELPER"}
DROPPED_PREFIXES = ("GH_", "GITHUB_", "CODEX_AUTOMATION_", "CODEX_GITHUB_")


class MentionSearchError(Exception):
    pass


def director_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    env = {key: value for key, value in source.items()
           if key in KEPT_WRAPPER_HOOKS or not key.startswith(DROPPED_PREFIXES)}
    env["CODEX_SKILLS_ENV_FILE"] = os.devnull
    env["GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK"] = "1"
    return env


def director_get(endpoint: str, run: Callable[..., Any] | None = None) -> Any:
    """GET one search or actor endpoint as the active gh login; nothing else."""
    if endpoint != "user" and not endpoint.startswith("search/issues?"):
        raise ValueError(f"Director-login reads are limited to search and actor endpoints: {endpoint}")
    result = (run or subprocess.run)([str(WRAPPER), "api", "--method", "GET", endpoint],
                 capture_output=True, text=True, env=director_env(), check=False)
    if result.returncode != 0:
        lines = (result.stderr or result.stdout or "").strip().splitlines()
        raise MentionSearchError(lines[-1] if lines else f"GitHub read failed with exit {result.returncode}")
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as error:
        raise MentionSearchError("GitHub returned invalid JSON") from error


def thread_comments_path(issue_api_url: str) -> str:
    """REST comment-list path for a comment's `issue_url`."""
    match = re.fullmatch(r"https://api\.github\.com/(repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+)", issue_api_url)
    if not match:
        raise ValueError("unexpected approval thread URL")
    return f"{match.group(1)}/comments"


def answer(comment: Any, director_login: str, recorders: set[str]) -> bool | None:
    """True or False for a Director answer to the approval question, else None."""
    if not isinstance(comment, dict) or not isinstance(comment.get("body"), str):
        return None
    author = ((comment.get("user") or {}).get("login") or "").casefold()
    line = next((line.strip() for line in comment["body"].splitlines() if line.strip()), "")
    if line.startswith(">"):
        return None
    text = line.strip("*_` ")
    decision = DECISION_PREFIX.match(text)
    if decision and author in {login.casefold() for login in recorders}:
        text = text[decision.end():].strip("*_` ")
    elif author != director_login.casefold():
        return None
    match = ANSWER.match(text)
    if not match:
        return None
    return match.group(1).casefold() in AFFIRMATIVE and not HEDGE.search(match.group(2))


def approval_gap(
    approval_url: str | None, director_login: str, recorders: set[str],
    read_comment: Callable[[str, int], Any], read_later: Callable[[str, str], list[Any]],
) -> str | None:
    """Why unattended Director-login searches are not approved, or None.

    `read_later(issue_api_url, since)` lists that thread's comments updated
    since the approval, so a later answer from the Director withdraws it.
    """
    if not approval_url:
        return f"no standing approval recorded ({APPROVAL_QUESTION} on {APPROVAL_ISSUE})"
    match = COMMENT_URL.fullmatch(approval_url.strip())
    if not match:
        return "the standing approval must be a GitHub issue comment link"
    try:
        comment = read_comment(match.group(1), int(match.group(2)))
        if answer(comment, director_login, recorders) is not True:
            return f"the linked comment is not an unqualified {APPROVAL_QUESTION} approval from the Director"
        later = read_later(comment["issue_url"], comment["created_at"])
    except Exception as error:  # Any read failure leaves approval unverified.
        return f"the standing approval could not be verified: {error}"
    answers = [answer(item, director_login, recorders) for item in sorted(
        (item for item in later if isinstance(item, dict) and item.get("id") != comment.get("id")),
        key=lambda item: str(item.get("created_at") or ""))]
    if [value for value in answers if value is not None][-1:] == [False]:
        return f"a later answer withdrew the {APPROVAL_QUESTION} approval"
    return None


def timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def search(
    director_login: str, since: str, *, until: str | None = None, excluded_repositories=(), excluded_owners=(),
    get: Callable[[str], Any] | None = None,
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """Return (mentions, coverage gap, read error) for threads updated in [since, until]."""
    get = get or director_get
    try:
        actor = get("user")
    except MentionSearchError as error:
        return [], None, str(error)
    login = actor.get("login") if isinstance(actor, dict) else None
    if not isinstance(login, str) or login.casefold() != director_login.casefold():
        return [], f"the active gh login is {login or 'unknown'}, not the Director's {director_login}", None
    excluded_repos = {repo.casefold() for repo in excluded_repositories}
    owners = sorted({owner.casefold() for owner in excluded_owners})
    query = " ".join([f"mentions:{director_login}", *(f"-user:{owner}" for owner in owners),
                      f"updated:{since}..{until}" if until else f"updated:>={since}"])
    mentions, fetched = [], 0
    try:
        for page in range(1, MAX_PAGES + 1):
            payload = get("search/issues?" + urlencode({"q": query, "per_page": PER_PAGE, "page": page}))
            if (not isinstance(payload, dict) or not isinstance(payload.get("items"), list)
                    or not isinstance(payload.get("total_count"), int)):
                raise MentionSearchError("invalid mention search response")
            items = payload["items"]
            fetched += len(items)
            for item in items:
                repo = "/".join(str(item.get("repository_url") or "").split("/")[-2:])
                url, updated, number = item.get("html_url"), item.get("updated_at"), item.get("number")
                if "/" not in repo or not isinstance(number, int) or not all(
                        isinstance(value, str) for value in (url, updated)):
                    raise MentionSearchError("mention search returned a thread without repository, number or link")
                if repo.casefold() in excluded_repos or repo.split("/")[0].casefold() in owners or (
                        until and timestamp(updated) > timestamp(until)):
                    continue
                user = item.get("user")
                mentions.append({
                    "repository": repo, "number": number, "url": url, "updated_at": updated,
                    "author": user.get("login") if isinstance(user, dict) else None,
                    "kind": "director_mention", "untrusted": True,
                })
            if payload.get("incomplete_results") or (
                    fetched < payload["total_count"] and (len(items) < PER_PAGE or page == MAX_PAGES)):
                raise MentionSearchError("mention search coverage is incomplete (GitHub search limit or partial results)")
            if len(items) < PER_PAGE or fetched >= payload["total_count"]:
                break
    except (MentionSearchError, ValueError) as error:
        return [], None, str(error)
    return mentions, None, None


def collect(
    director_login: str | None, since: str, approval_url: str | None, recorders: set[str],
    read_comment: Callable[[str, int], Any], read_later: Callable[[str, str], list[Any]], **scope: Any,
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """Run the approved search, or explain the coverage gap without searching."""
    if not director_login:
        return [], "no Director login configured for mention searches", None
    gap = approval_gap(approval_url, director_login, recorders, read_comment, read_later)
    if gap:
        return [], gap, None
    return search(director_login, since, **scope)
