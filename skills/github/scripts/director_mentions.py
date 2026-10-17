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
from typing import Any, Callable
from urllib.parse import urlencode

APPROVAL_QUESTION = "Q455"
APPROVAL_ISSUE = "https://github.com/cbusillo/codex-skills/issues/2073"
APPROVAL = re.compile(rf"\b{APPROVAL_QUESTION}\b\W+(?:yes|approved)\b", re.IGNORECASE)
DECISION_RECORD = re.compile(r"^\W*Director decision\b", re.IGNORECASE)
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


def approval_gap(
    approval_url: str | None, director_login: str, recorders: set[str],
    read_comment: Callable[[str, int], Any],
) -> str | None:
    """Why unattended Director-login searches are not approved, or None."""
    if not approval_url:
        return f"no standing approval recorded ({APPROVAL_QUESTION} on {APPROVAL_ISSUE})"
    match = COMMENT_URL.fullmatch(approval_url.strip())
    if not match:
        return "the standing approval must be a GitHub issue comment link"
    try:
        comment = read_comment(match.group(1), int(match.group(2)))
    except Exception as error:  # Any read failure leaves approval unverified.
        return f"the standing approval comment could not be read: {error}"
    if not isinstance(comment, dict):
        return "the standing approval comment could not be read"
    author = ((comment.get("user") or {}).get("login") or "").casefold()
    body = comment.get("body") or ""
    if not isinstance(body, str) or not APPROVAL.search(body):
        return f"the linked comment does not approve {APPROVAL_QUESTION}"
    if author == director_login.casefold():
        return None
    if author in {login.casefold() for login in recorders} and DECISION_RECORD.match(body):
        return None
    return "the linked approval is neither the Director's reply nor a recorded Director decision"


def search(
    director_login: str, since: str, *, excluded_repositories=(), excluded_owners=(),
    get: Callable[[str], Any] | None = None,
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """Return (mentions, coverage gap, read error) for threads updated since `since`."""
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
                      f"updated:>={since}"])
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
                if repo.casefold() in excluded_repos or repo.split("/")[0].casefold() in owners:
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
    except MentionSearchError as error:
        return [], None, str(error)
    return mentions, None, None


def collect(
    director_login: str | None, since: str, approval_url: str | None, recorders: set[str],
    read_comment: Callable[[str, int], Any], **scope: Any,
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """Run the approved search, or explain the coverage gap without searching."""
    if not director_login:
        return [], "no Director login configured for mention searches", None
    gap = approval_gap(approval_url, director_login, recorders, read_comment)
    if gap:
        return [], gap, None
    return search(director_login, since, **scope)
