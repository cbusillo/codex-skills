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
import sys
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from skills.direction.scripts import question_id


QUESTION_ID = re.compile(r"Q\d+\b", re.IGNORECASE)
# An answer is the comment's first line, unquoted: "Q<number> yes" from the Director,
# or "Director decision: Q<number> yes" recorded by trusted automation. Any other
# answer to the question, or a hedged one, withholds approval.
DECISION_PREFIX = re.compile(r"Director decision\s*:\s*", re.IGNORECASE)
AFFIRMATIVE = {"yes", "approved", "approve"}
HEDGE = re.compile(r"\b(?:not|no|don't|do not|but|unless|declin\w*|reject\w*|revok\w*|withdraw\w*)\b", re.IGNORECASE)
UNQUALIFIED_TAIL = re.compile(r"[\s*_`.,!]*(?:(?:go ahead|again|mention searches)[\s*_`.,!]*)?", re.IGNORECASE)
# A direct permission clause, not a bag of search-related words. Unsupported
# phrasing reports a coverage gap; it cannot silently become standing permission.
PERMISSION_SUBJECT = r"(?:(?:(?:the|my|your|our)\s+)?(?:(?:supervisor['’]s|comment|mention|GitHub)\s+)?(?:supervisor|watcher|watch|radar|agents?|tools?)|I|we)"
MENTION_PERMISSION = re.compile(
    rf"^(?:may|can|should|could|will)\s+{PERMISSION_SUBJECT}(?:\s+and\s+{PERMISSION_SUBJECT})*\s+"
    r"(?:search(?:\s+GitHub)?\s+(?:for\s+)?mentions?|"
    r"run\s+(?:(?:unattended|read-only|GitHub)\s+)*mention\s+searches)"
    r"(?:\s+of\s+you)?\s+(?:under|using|with|as)\s+"
    r"(?:your|my|the\s+Director['’]s)\s+(?:own\s+)?(?:gh\s+|GitHub\s+)?login"
    r"(?:[,;]?\s+(?:unattended|read-only|on\s+(?:every|each)\s+(?:pass|run)))*\s*[?.!]?", re.IGNORECASE)
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


def answer(comment: Any, director_login: str, recorders: set[str], *,
           question: str, recorded_affirmative: str | None = None, unquestioned: bool = False) -> bool | None:
    """True or False for a Director answer, else None.

    Other narrow readers may name their own question and the exact affirmative
    first line of an existing trusted recorded decision.
    """
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
        if recorded_affirmative and text.casefold() == recorded_affirmative.casefold():
            return True
    elif author != director_login.casefold():
        return None
    match = re.match(rf"{re.escape(question)}\b\W*(\w+)(.*)", text, re.IGNORECASE)
    if not match:
        return None
    return (match.group(1).casefold() in AFFIRMATIVE and not HEDGE.search(match.group(2))
            and (not unquestioned or UNQUALIFIED_TAIL.fullmatch(match.group(2)) is not None))


def approval_gap(
    approval_url: str | None, director_login: str, recorders: set[str],
    read_comment: Callable[[str, int], Any], read_later: Callable[[str, str], list[Any]],
    *, question: str | None = None, recorded_affirmative: str | None = None,
) -> str | None:
    """Why unattended Director-login searches are not approved, or None.

    `read_later(issue_api_url, since)` lists that thread's comments updated
    since a timestamp. Mention permission needs the whole thread to bind the
    question; explicit-question readers retain their existing narrower read.
    """
    if not approval_url:
        return "no standing approval recorded for mention searches"
    match = COMMENT_URL.fullmatch(approval_url.strip())
    if not match:
        return "the standing approval must be a GitHub issue comment link"
    try:
        comment = read_comment(match.group(1), int(match.group(2)))
        mention_permission = question is None
        if question is None:
            # Read the whole thread, including the question before the approval.
            # Explicit-question consumers (such as attachments) keep their own gate.
            thread = read_later(comment["issue_url"], "1970-01-01T00:00:00Z")
            question = mention_question(comment, thread, approval_url.strip(), director_login, recorders)
            if question is None:
                return "the linked approval has no matching mention-search permission question"
        else:
            thread = read_later(comment["issue_url"], comment["created_at"])
        if not answer(comment, director_login, recorders, question=question,
                      recorded_affirmative=recorded_affirmative, unquestioned=mention_permission):
            return f"the linked comment is not an unqualified {question} approval from the Director"
    except Exception as error:  # Any read failure leaves approval unverified.
        return f"the standing approval could not be verified: {error}"
    answers = [answer(item, director_login, recorders, question=question,
                      recorded_affirmative=recorded_affirmative, unquestioned=mention_permission) for item in sorted(
        (item for item in thread if isinstance(item, dict) and item.get("id") != comment.get("id")
         and str(item.get("updated_at") or item.get("created_at") or "") >= comment["created_at"]),
        key=lambda item: str(item.get("created_at") or ""))]
    if [value for value in answers if value is not None][-1:] == [False]:
        return f"a later answer withdrew the {question} approval"
    return None


def mention_question(comment: Any, thread: list[Any], approval_url: str,
                     director_login: str, recorders: set[str]) -> str | None:
    """Bind an approval to its trusted, preceding permission heading in this thread."""
    linked = COMMENT_URL.fullmatch(approval_url)
    if not isinstance(comment, dict) or not linked:
        return None
    expected_thread = approval_url.split("#", 1)[0].replace("https://github.com/", "https://api.github.com/repos/")
    expected_thread = expected_thread.replace("/pull/", "/issues/")
    if comment.get("id") != int(linked.group(2)) or str(comment.get("issue_url") or "").casefold() != expected_thread.casefold():
        return None
    first_line = next((line.strip() for line in str(comment.get("body") or "").splitlines() if line.strip()), "")
    text = DECISION_PREFIX.sub("", first_line.strip("*_` "), count=1).strip("*_` ")
    approved_id = QUESTION_ID.match(text)
    if not approved_id:
        return None
    question = approved_id.group().upper()
    trusted = {login.casefold() for login in {*recorders, director_login}}
    candidates = []
    for item in thread:
        if not isinstance(item, dict) or not isinstance(item.get("body"), str):
            continue
        if ((item.get("user") or {}).get("login") or "").casefold() not in trusted:
            continue
        if str(item.get("issue_url", expected_thread)).casefold() != expected_thread.casefold() or not (
                str(item.get("created_at") or "") < comment["created_at"]):
            continue
        # The posting flow's heading is the first physical line, never body prose.
        heading = next((line.strip() for line in item["body"].splitlines() if line.strip()), "")
        heading = re.sub(r"^#{1,6}\s+", "", heading).replace("*", "").replace("`", "")
        if question_id.heading_id(heading) == question:
            scope = heading.split(question, 1)[1].lstrip(" .:;—–-")
            unchanged = str(item.get("updated_at") or item.get("created_at") or "") <= comment["created_at"]
            candidates.append(bool(unchanged and MENTION_PERMISSION.fullmatch(scope)))
    if not candidates or not all(candidates):
        return None
    return question


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
