#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Find external GitHub comments that still need the owner's attention."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import github_work_rollup as rollup

IDENTITY_SCRIPT_DIR = ROOT / "github" / "scripts"
if str(IDENTITY_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(IDENTITY_SCRIPT_DIR))
import director_mentions
import github_identity
import github_direction_next
import github_plan_wait
import github_repository_scope as repository_scope


def load_sibling_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resolve_person = load_sibling_module("resolve_person", ROOT / "people" / "scripts" / "resolve_person.py")
director_questions = load_sibling_module("oq", ROOT / "supervisor" / "scripts" / "oq.py")


GH =os.environ.get("GITHUB_UNANSWERED_COMMENTS_GH") or rollup.GH
DEFAULT_CONFIG = ROOT / ".local/github-work-rollup.yaml"
DEFAULT_WINDOW = "30d"
DEFAULT_BOT_LOGINS = ()
MAX_WORKERS = 8
EXIT_CLEAR = 0
EXIT_ATTENTION = 2
EXIT_DEGRADED = 3
OPENING_POST = "issue_body"
WAITING_LABEL = "plan:waiting"
PLAN_RECORD_MARKER = "<!-- github-plan:"
DIRECTOR_ROLE_WORDS = ("Director",)
POSSIBLE_AUTOMATION_LOGIN = re.compile(r"(?:^|[-_])(?:bot|automation)$", re.IGNORECASE)
REACTIONS_QUERY = """
query($id: ID!, $after: String) {
  node(id: $id) {
    ... on Comment { lastEditedAt editor { login } }
    ... on Reactable {
      reactions(first: 100, after: $after, orderBy: {field: CREATED_AT, direction: ASC}) {
        nodes {
          content
          createdAt
          user { login }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


@dataclass(frozen=True)
class Comment:
    repo: str
    number: int
    kind: str
    comment_id: int
    author: str
    author_type: str
    created_at: str
    updated_at: str
    body: str
    url: str
    node_id: str = ""
    in_reply_to_id: int | None = None

    @property
    def key(self) -> tuple[str, int]:
        return self.kind, self.comment_id

    @property
    def thread_key(self) -> tuple[str, int]:
        return self.repo, self.number

    @property
    def global_key(self) -> tuple[str, int, str, int]:
        return self.repo, self.number, self.kind, self.comment_id


@dataclass(frozen=True)
class Reaction:
    actor: str
    content: str
    created_at: str


@dataclass(frozen=True)
class Edit:
    at: str
    editor: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report external issue and PR comments that still need the owner's attention."
    )
    repository_scope.add_include_archived_argument(parser)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Optional GitHub work rollup YAML config.")
    parser.add_argument("--repo", action="append", default=[], help="OWNER/REPO. May be repeated.")
    parser.add_argument("--repo-owner", action="append", default=[], help="Owner whose repos should be scanned, excluding archives by default.")
    parser.add_argument("--thread", action="append", default=[], help="Full-history OWNER/REPO#NUMBER or GitHub issue/PR URL.")
    parser.add_argument("--outbound", action="store_true", help="Find replies after your last activity in repositories you do not own.")
    parser.add_argument("--self-login", action="append", default=[], help="Human owner login whose reactions and replies count.")
    parser.add_argument(
        "--director-login",
        help="The Director's login. Configured, authenticated and repository-owner logins are then not "
        "treated as the Director, so another account's own comments on its threads count as outside comments.",
    )
    parser.add_argument(
        "--mention-approval",
        help="Link to the Director's standing approval for --outbound mention searches under their own login.",
    )
    parser.add_argument("--bot-login", action="append", default=[], help="Automation login whose targeted replies count.")
    parser.add_argument(
        "--people-index",
        type=Path,
        help="Private people YAML naming the Director and other people's automation accounts; defaults to the people skill's index.",
    )
    parser.add_argument("--window", help="Lookback such as 24h, 30d, or 12w.")
    parser.add_argument("--since", help="UTC ISO timestamp for the scan start.")
    parser.add_argument("--until", help="UTC ISO timestamp for the scan end.")
    parser.add_argument("--limit-repos", type=int, default=1000)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument(
        "--include-handled",
        action="store_true",
        help="Also list handled comments in the JSON report, for callers that apply their own rule.",
    )
    parser.add_argument("--output", type=Path, help="Write the report to this path.")
    args = parser.parse_args()
    if args.limit_repos < 1:
        parser.error("--limit-repos must be > 0")
    return args


def resolve_settings(
    args: argparse.Namespace,
    config: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    threads = unique_threads(parse_thread_reference(value) for value in args.thread)
    outbound = bool(getattr(args, "outbound", False))
    explicit_scope = bool(args.repo or args.repo_owner or threads or outbound)
    repositories = rollup.unique(args.repo if explicit_scope else rollup.as_str_list(config.get("repositories")))
    repo_owners = rollup.unique(args.repo_owner if explicit_scope else rollup.as_str_list(config.get("repo_owners")))
    director_login = getattr(args, "director_login", None)
    configured_self = rollup.as_str_list(config.get("comment_self_logins"))
    if not configured_self:
        configured_self = rollup.as_str_list(config.get("subjects"))
    # A named Director replaces every inferred or configured identity.
    self_logins = rollup.unique([*([] if director_login else configured_self), *args.self_login])
    outbound_self_logins = list(self_logins)
    configured_login = github_identity.automation_login()
    bot_logins = rollup.unique(
        [
            *DEFAULT_BOT_LOGINS,
            *github_identity.configured_bot_logins(),
            *([configured_login] if configured_login else []),
            *rollup.as_str_list(config.get("comment_bot_logins")),
            *args.bot_login,
        ]
    )
    inferred_self = ([director_login] if director_login
                     else [*repo_owners, *(repo.split("/", 1)[0] for repo in repositories if "/" in repo)])
    self_logins = rollup.unique([*self_logins, *inferred_self])

    until = rollup.parse_timestamp(args.until) or now or datetime.now(timezone.utc)
    window_label = args.window or str(config.get("comment_window") or DEFAULT_WINDOW)
    since = rollup.parse_timestamp(args.since)
    if since is None:
        since = until - timedelta(hours=rollup.parse_duration_hours(window_label))
    if since >= until:
        raise SystemExit("error: --since must be before --until")

    return {
        "config_path": str(args.config),
        "repositories": repositories,
        "repo_owners": repo_owners,
        "threads": threads,
        "scan_repository_scope": bool(repositories or repo_owners or not explicit_scope),
        "outbound": outbound,
        "outbound_self_logins": outbound_self_logins,
        "mention_approval": getattr(args, "mention_approval", None) or config.get("mention_approval"),
        "explicit_scope": explicit_scope,
        "self_logins": self_logins,
        "director_login": director_login,
        "bot_logins": bot_logins,
        "since": since.astimezone(timezone.utc),
        "until": until.astimezone(timezone.utc),
        "window_label": window_label,
        "limit_repos": args.limit_repos,
        "include_archived": bool(getattr(args, "include_archived", False)),
        "include_handled": bool(getattr(args, "include_handled", False)),
        "people_index": args.people_index or (Path(str(config["people_index"])) if config.get("people_index") else None),
    }


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def run_json(command: list[str]) -> Any:
    result = run(command)
    if result.returncode != 0:
        detail = rollup.trim(result.stderr or result.stdout)
        raise rollup.RollupError(f"GitHub command failed: {' '.join(command)}\n{detail}")
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise rollup.RollupError(f"GitHub command returned invalid JSON: {' '.join(command)}") from exc


def api_list(endpoint: str) -> list[dict[str, Any]]:
    payload = run_json([GH, "api", endpoint, "--paginate", "--slurp"])
    if not isinstance(payload, list):
        raise rollup.RollupError(f"GitHub returned an invalid paginated payload for {endpoint}.")
    if all(isinstance(page, list) for page in payload):
        pages = payload
    elif all(isinstance(item, dict) for item in payload):
        pages = [payload]
    else:
        raise rollup.RollupError(f"GitHub returned a mixed paginated payload for {endpoint}.")
    return [item for page in pages for item in page if isinstance(item, dict)]


def authenticated_login() -> str:
    payload = run_json([GH, "api", "user"])
    return str(payload.get("login") or "") if isinstance(payload, dict) else ""


def current_repository() -> str:
    payload = run_json([GH, "repo", "view", "--json", "nameWithOwner"])
    if not isinstance(payload, dict) or not payload.get("nameWithOwner"):
        raise rollup.RollupError("Unable to resolve the current GitHub repository.")
    return str(payload["nameWithOwner"])


def parse_thread_reference(value: str) -> tuple[str, int]:
    text = value.strip()
    short_match = re.fullmatch(r"([^/\s]+/[^#\s]+)#(\d+)", text)
    if short_match:
        return short_match.group(1), int(short_match.group(2))
    url_match = re.fullmatch(
        r"https?://github\.com/([^/\s]+)/([^/\s]+)/(?:issues|pull)/(\d+)(?:[/?#].*)?",
        text,
    )
    if url_match:
        return f"{url_match.group(1)}/{url_match.group(2)}", int(url_match.group(3))
    raise SystemExit(f"error: unsupported --thread value {value!r}")


def unique_threads(values: Any) -> list[tuple[str, int]]:
    seen: set[tuple[str, int]] = set()
    result: list[tuple[str, int]] = []
    for repo, number in values:
        key = (repo.casefold(), number)
        if key in seen:
            continue
        seen.add(key)
        result.append((repo, number))
    return result


def resolve_repositories(settings: dict[str, Any]) -> list[str]:
    def read_metadata(repo):
        metadata = run_json([GH, "repo", "view", repo, "--json", "isArchived"])
        if not isinstance(metadata, dict):
            raise rollup.RollupError(f"Invalid repository metadata for {repo}")
        return metadata

    repositories = repository_scope.filter_named_repositories(
        settings["repositories"], read_metadata,
        include_archived=settings.get("include_archived", False),
    )
    settings["excluded_archived_repositories"] = [repo for repo in settings["repositories"] if repo not in repositories]
    repositories.extend(repo for repo, _ in settings["threads"])
    for owner in settings["repo_owners"]:
        payload = run_json(
            [
                GH,
                "repo",
                "list",
                owner,
                "--limit",
                str(settings["limit_repos"]),
                "--json",
                "nameWithOwner,isArchived",
            ]
        )
        if not isinstance(payload, list):
            continue
        if len(payload) >= settings["limit_repos"]:
            raise rollup.RollupError(
                f"Repository scan for {owner} reached --limit-repos={settings['limit_repos']}; coverage is incomplete."
            )
        visible_repositories = [
            str(item["nameWithOwner"])
            for item in payload
            if isinstance(item, dict) and repository_scope.includes_repository(item, include_archived=settings.get("include_archived", False)) and item.get("nameWithOwner")
        ]
        settings["excluded_archived_repositories"].extend(
            str(item["nameWithOwner"]) for item in payload
            if isinstance(item, dict) and item.get("nameWithOwner") and str(item["nameWithOwner"]) not in visible_repositories
        )
        if not visible_repositories:
            raise rollup.RollupError(f"Repository owner scope {owner!r} resolved to no visible repositories in the selected archive scope.")
        repositories.extend(
            visible_repositories
        )
    if not repositories:
        if settings.get("outbound") and not settings["scan_repository_scope"]:
            return []
        if settings["explicit_scope"] or settings["repositories"] or settings["repo_owners"]:
            raise rollup.RollupError("The explicit repository scope resolved to no repositories; use --include-archived for archived repositories.")
        repositories.append(current_repository())
    return rollup.unique(repositories)


def normalize_login(value: object) -> str:
    return str(value or "").strip().casefold().removeprefix("@")


def github_logins(person: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return a person's own GitHub logins and the automation logins that act for them."""
    own: list[str] = []
    automation: list[str] = []
    entries = [person.get("github")]
    contacts = person.get("contacts")
    if isinstance(contacts, dict):
        entries.append(contacts.get("github"))
    for entry in entries:
        if isinstance(entry, str):
            own.append(entry)
        elif isinstance(entry, dict):
            own.extend(rollup.as_str_list(entry.get("username")) + rollup.as_str_list(entry.get("handle")))
            automation.extend(rollup.as_str_list(entry.get("bot_usernames")) + rollup.as_str_list(entry.get("bot_handles")))
    return (
        [login for login in map(normalize_login, own) if login],
        [login for login in map(normalize_login, automation) if login],
    )


def people_identities(self_logins: set[str], index_path: Path | None) -> dict[str, Any]:
    """Read who acts for whom from the people index.

    Index entries only label accounts: they never exclude anyone from the
    report, since the Director is inferred from scanned repository owners.
    Only configured automation (--bot-login and its sources) is excluded.
    """
    empty = {"director_names": [], "director_ambiguous": False, "automation": {}}
    if index_path is not None and not index_path.expanduser().is_file():
        # An index someone named must exist; only the default index is optional.
        return {**empty, "status": "error", "error": f"people index {index_path} does not exist"}
    try:
        if index_path is not None:
            status, people, _sources = resolve_person.load_scoped_people(scope="global", global_index=index_path)
        else:
            status, people, _sources = resolve_person.load_scoped_people()
    except resolve_person.PeopleConfigError as exc:
        return {**empty, "status": "error", "error": str(exc)}
    directors = [person for person in people if set(github_logins(person)[0]) & self_logins]
    automation: dict[str, str] = {}
    for person in people:
        label = str(person.get("preferred_reference") or person.get("display_name") or person.get("id") or "")
        for login in github_logins(person)[1]:
            automation[login] = label
    if len(directors) != 1:
        # Scanning another person's repository makes them match too; use no
        # names rather than guess which match is the Director.
        return {**empty, "status": status, "director_ambiguous": len(directors) > 1, "automation": automation}
    [director] = directors
    names = [
        *rollup.as_str_list(director.get("display_name")),
        *rollup.as_str_list(director.get("preferred_reference")),
        *rollup.as_str_list(director.get("aliases")),
    ]
    return {**empty, "status": status, "director_names": rollup.unique(names), "automation": automation}


def author_class(author: str, automation_logins: dict[str, str]) -> tuple[str, str | None]:
    """Say whether an external author is a person, a known automation account, or looks like automation."""
    login = normalize_login(author)
    if login in automation_logins:
        return "automation", automation_logins[login] or None
    if POSSIBLE_AUTOMATION_LOGIN.search(login):
        return "possible_automation", None
    return "person", None


def is_github_bot(author: str, author_type: str) -> bool:
    login = normalize_login(author)
    return normalize_login(author_type) == "bot" or login.endswith("[bot]")


def is_external_comment(comment: Comment, self_logins: set[str], bot_logins: set[str]) -> bool:
    author = normalize_login(comment.author)
    # Opening an issue or PR needs attention even with an empty description.
    if not author or (not comment.body.strip() and comment.kind != OPENING_POST):
        return False
    if author in self_logins or author in bot_logins:
        return False
    return not is_github_bot(comment.author, comment.author_type)


def comment_timestamp(value: str) -> datetime:
    return rollup.parse_timestamp(value) or datetime.min.replace(tzinfo=timezone.utc)


def comment_activity_time(comment: Comment) -> datetime:
    return max(comment_timestamp(comment.created_at), comment_timestamp(comment.updated_at))


def comment_in_window(comment: Comment, since: datetime, until: datetime) -> bool:
    created_at = comment_timestamp(comment.created_at)
    updated_at = comment_timestamp(comment.updated_at)
    return since <= created_at <= until or since <= updated_at <= until


def parse_number(url: object) -> int | None:
    match = re.search(r"/(?:issues|pulls)/(\d+)(?:$|[/?#])", str(url or ""))
    return int(match.group(1)) if match else None


def normalize_issue_comment(repo: str, item: dict[str, Any]) -> Comment | None:
    number = parse_number(item.get("issue_url"))
    comment_id = item.get("id")
    if number is None or not isinstance(comment_id, int):
        return None
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    return Comment(
        repo=repo,
        number=number,
        kind="issue_comment",
        comment_id=comment_id,
        author=str(user.get("login") or ""),
        author_type=str(user.get("type") or ""),
        created_at=str(item.get("created_at") or ""),
        updated_at=str(item.get("updated_at") or item.get("created_at") or ""),
        body=str(item.get("body") or ""),
        url=str(item.get("html_url") or ""),
        node_id=str(item.get("node_id") or ""),
    )


def normalize_review_comment(repo: str, item: dict[str, Any]) -> Comment | None:
    number = parse_number(item.get("pull_request_url"))
    comment_id = item.get("id")
    if number is None or not isinstance(comment_id, int):
        return None
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    reply_id = item.get("in_reply_to_id")
    return Comment(
        repo=repo,
        number=number,
        kind="review_comment",
        comment_id=comment_id,
        author=str(user.get("login") or ""),
        author_type=str(user.get("type") or ""),
        created_at=str(item.get("created_at") or ""),
        updated_at=str(item.get("updated_at") or item.get("created_at") or ""),
        body=str(item.get("body") or ""),
        url=str(item.get("html_url") or ""),
        node_id=str(item.get("node_id") or ""),
        in_reply_to_id=reply_id if isinstance(reply_id, int) else None,
    )


def normalize_opening_post(repo: str, item: dict[str, Any]) -> Comment | None:
    """Treat the issue or PR description as its author's first message.

    Its edit time comes from GraphQL ``lastEditedAt``: the REST ``updated_at``
    moves with every comment and label change.
    """
    number = item.get("number")
    thread_id = item.get("id")
    if not isinstance(number, int) or not isinstance(thread_id, int):
        return None
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    created_at = str(item.get("created_at") or "")
    return Comment(
        repo=repo,
        number=number,
        kind=OPENING_POST,
        comment_id=thread_id,
        author=str(user.get("login") or ""),
        author_type=str(user.get("type") or ""),
        created_at=created_at,
        updated_at=created_at,
        body=str(item.get("body") or ""),
        url=str(item.get("html_url") or ""),
        node_id=str(item.get("node_id") or ""),
    )


def normalize_review(repo: str, number: int, item: dict[str, Any]) -> Comment | None:
    review_id = item.get("id")
    if not isinstance(review_id, int):
        return None
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    submitted_at = str(item.get("submitted_at") or "")
    return Comment(
        repo=repo,
        number=number,
        kind="review",
        comment_id=review_id,
        author=str(user.get("login") or ""),
        author_type=str(user.get("type") or ""),
        created_at=submitted_at,
        updated_at=submitted_at,
        body=str(item.get("body") or ""),
        url=str(item.get("html_url") or ""),
        node_id=str(item.get("node_id") or ""),
    )


def format_api_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def opened_threads(repo: str, since: datetime, until: datetime, *, per_page: int = 100) -> list[dict[str, Any]]:
    """Read issues and PRs created in the window, newest first, stopping at ``since``."""
    opened: list[dict[str, Any]] = []
    page = 1
    while True:
        query = urlencode({"state": "all", "sort": "created", "direction": "desc", "per_page": per_page, "page": page})
        payload = run_json([GH, "api", f"repos/{repo}/issues?{query}"])
        if not isinstance(payload, list):
            raise rollup.RollupError(f"GitHub returned an invalid issue page for {repo}.")
        items = [item for item in payload if isinstance(item, dict)]
        for item in items:
            created_at = rollup.parse_timestamp(str(item.get("created_at") or ""))
            if created_at is None:
                raise rollup.RollupError(f"GitHub returned an issue without a creation time for {repo}.")
            if created_at < since:
                return opened
            if created_at <= until:
                opened.append(item)
        if len(payload) < per_page:
            return opened
        page += 1


def repository_activity(
    repo: str,
    since: datetime,
    until: datetime,
    self_logins: set[str],
    bot_logins: set[str],
) -> tuple[dict[tuple[str, int], set[tuple[str, int]]], list[dict[str, str]]]:
    query = urlencode(
        {
            "since": format_api_timestamp(since),
            "sort": "updated",
            "direction": "asc",
            "per_page": 100,
        }
    )
    candidate_ids: dict[tuple[str, int], set[tuple[str, int]]] = {}
    errors: list[dict[str, str]] = []
    lanes = (
        ("issue_comments", f"repos/{repo}/issues/comments?{query}", normalize_issue_comment),
        ("review_comments", f"repos/{repo}/pulls/comments?{query}", normalize_review_comment),
    )
    for lane, endpoint, normalizer in lanes:
        try:
            items = api_list(endpoint)
        except rollup.RollupError as exc:
            errors.append({"repo": repo, "lane": lane, "error": rollup.trim(str(exc))})
            continue
        for item in items:
            comment = normalizer(repo, item)
            if (
                comment is None
                or not comment_in_window(comment, since, until)
                or not is_external_comment(comment, self_logins, bot_logins)
            ):
                continue
            candidate_ids.setdefault(comment.thread_key, set()).add(comment.key)
    try:
        threads = opened_threads(repo, since, until)
    except rollup.RollupError as exc:
        errors.append({"repo": repo, "lane": "opened_threads", "error": rollup.trim(str(exc))})
        threads = []
    for item in threads:
        opening = normalize_opening_post(repo, item)
        if opening is not None and is_external_comment(opening, self_logins, bot_logins):
            candidate_ids.setdefault(opening.thread_key, set()).add(opening.key)
    return candidate_ids, errors


def collect_thread(
    repo: str,
    number: int,
) -> tuple[dict[str, Any] | None, list[Comment], list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    try:
        thread = run_json([GH, "api", f"repos/{repo}/issues/{number}"])
    except rollup.RollupError as exc:
        return None, [], [{"repo": repo, "lane": "thread", "error": rollup.trim(str(exc))}]
    if not isinstance(thread, dict):
        return None, [], [{"repo": repo, "lane": "thread", "error": "GitHub returned an invalid thread payload."}]

    comments: list[Comment] = []
    opening = normalize_opening_post(repo, thread)
    if opening is not None:
        comments.append(opening)
    try:
        issue_items = api_list(f"repos/{repo}/issues/{number}/comments?per_page=100")
        comments.extend(
            comment
            for item in issue_items
            if (comment := normalize_issue_comment(repo, item)) is not None
        )
    except rollup.RollupError as exc:
        errors.append({"repo": repo, "lane": "issue_comments", "error": rollup.trim(str(exc))})

    if isinstance(thread.get("pull_request"), dict):
        try:
            review_items = api_list(f"repos/{repo}/pulls/{number}/comments?per_page=100")
            comments.extend(
                comment
                for item in review_items
                if (comment := normalize_review_comment(repo, item)) is not None
            )
        except rollup.RollupError as exc:
            errors.append({"repo": repo, "lane": "review_comments", "error": rollup.trim(str(exc))})
        try:
            review_items = api_list(f"repos/{repo}/pulls/{number}/reviews?per_page=100")
            comments.extend(
                review
                for item in review_items
                if (review := normalize_review(repo, number, item)) is not None
            )
        except rollup.RollupError as exc:
            errors.append({"repo": repo, "lane": "reviews", "error": rollup.trim(str(exc))})

    return thread, comments, errors


def collect_comment_reactions(comment: Comment) -> tuple[list[Reaction], Edit | None]:
    if not comment.node_id:
        raise rollup.RollupError(f"Comment {comment.comment_id} has no GraphQL node id.")
    reactions: list[Reaction] = []
    last_edit = None
    after = ""
    seen_cursors: set[str] = set()
    while True:
        command = [GH, "api", "graphql", "-f", f"query={REACTIONS_QUERY}", "-F", f"id={comment.node_id}"]
        if after:
            command.extend(["-F", f"after={after}"])
        payload = run_json(command)
        if not isinstance(payload, dict) or payload.get("errors"):
            raise rollup.RollupError(f"GitHub returned an invalid reaction payload for comment {comment.comment_id}.")
        data = payload.get("data")
        node = data.get("node") if isinstance(data, dict) else None
        connection = node.get("reactions") if isinstance(node, dict) else None
        if not isinstance(connection, dict):
            raise rollup.RollupError(f"GitHub did not expose reactions for comment {comment.comment_id}.")
        node_last_edited_at = str(node.get("lastEditedAt") or "")
        if node_last_edited_at:
            editor = node.get("editor") or {}
            last_edit = Edit(node_last_edited_at, str(editor.get("login") or ""))
        nodes = connection.get("nodes")
        if not isinstance(nodes, list):
            raise rollup.RollupError(f"GitHub returned invalid reactions for comment {comment.comment_id}.")
        for item in nodes:
            if not isinstance(item, dict):
                continue
            user = item.get("user") if isinstance(item.get("user"), dict) else {}
            actor = str(user.get("login") or "")
            created_at = str(item.get("createdAt") or "")
            if actor and created_at:
                reactions.append(
                    Reaction(
                        actor=actor,
                        content=str(item.get("content") or ""),
                        created_at=created_at,
                    )
                )
        page_info = connection.get("pageInfo")
        if not isinstance(page_info, dict) or not page_info.get("hasNextPage"):
            return reactions, last_edit
        next_cursor = str(page_info.get("endCursor") or "")
        if not next_cursor or next_cursor in seen_cursors:
            raise rollup.RollupError(f"GitHub reaction pagination stalled for comment {comment.comment_id}.")
        seen_cursors.add(next_cursor)
        after = next_cursor


def collect_candidate_reactions(
    comments: list[Comment],
) -> tuple[
    dict[tuple[str, int, str, int], list[Reaction]],
    dict[tuple[str, int, str, int], Edit],
    list[dict[str, str]],
]:
    unique_comments = {comment.global_key: comment for comment in comments}
    reactions: dict[tuple[str, int, str, int], list[Reaction]] = {}
    last_edits: dict[tuple[str, int, str, int], Edit] = {}
    errors: list[dict[str, str]] = []
    if not unique_comments:
        return reactions, last_edits, errors
    worker_count = min(MAX_WORKERS, max(1, len(unique_comments)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(collect_comment_reactions, comment): comment
            for comment in unique_comments.values()
        }
        for future in as_completed(futures):
            comment = futures[future]
            try:
                comment_reactions, last_edit = future.result()
                reactions[comment.global_key] = comment_reactions
                if last_edit:
                    last_edits[comment.global_key] = last_edit
            except rollup.RollupError as exc:
                errors.append(
                    {
                        "repo": comment.repo,
                        "lane": f"reactions/{comment.kind}/{comment.comment_id}",
                        "error": rollup.trim(str(exc)),
                    }
                )
    return reactions, last_edits, errors


def event_sort_key(comment: Comment) -> tuple[str, int]:
    return comment.created_at, comment.comment_id


def comment_activity_through(comment: Comment, until: datetime) -> datetime:
    created_at = comment_timestamp(comment.created_at)
    updated_at = comment_timestamp(comment.updated_at)
    return max(created_at, updated_at if updated_at <= until else created_at)


def response_is_after(response: Comment, external: Comment, until: datetime) -> bool:
    response_time = comment_activity_through(response, until)
    external_time = comment_activity_through(external, until)
    if response_time != external_time:
        return response_time > external_time
    return (
        comment_timestamp(response.updated_at) == comment_timestamp(response.created_at)
        and comment_timestamp(external.updated_at) == comment_timestamp(external.created_at)
        and response.comment_id > external.comment_id
    )


def reaction_is_after(reaction: Reaction, external: Comment, until: datetime) -> bool:
    reaction_time = comment_timestamp(reaction.created_at)
    return reaction_time <= until and reaction_time > comment_activity_through(external, until)


def mentions_login(body: str, login: str) -> bool:
    escaped = re.escape(login.removeprefix("@"))
    return bool(re.search(rf"(?<![A-Za-z0-9-])@{escaped}(?![A-Za-z0-9-])", body, flags=re.IGNORECASE))


def review_thread_root(comment: Comment) -> int | None:
    if comment.kind != "review_comment":
        return None
    return comment.in_reply_to_id or comment.comment_id


def exact_comment_reference(response: Comment, external: Comment) -> bool:
    if external.kind == OPENING_POST:
        # The thread URL followed by a fragment links a comment, not the description.
        if external.url and re.search(rf"{re.escape(external.url)}(?![A-Za-z0-9/#-])", response.body):
            return True
        return bool(re.search(rf"#issue-{external.comment_id}(?!\d)", response.body))
    if external.url and re.search(rf"{re.escape(external.url)}(?![A-Za-z0-9])", response.body):
        return True
    fragments = (
        rf"issuecomment-{external.comment_id}(?!\d)",
        rf"discussion_r{external.comment_id}(?!\d)",
        rf"pullrequestreview-{external.comment_id}(?!\d)",
    )
    return any(re.search(fragment, response.body) for fragment in fragments)


def response_targets(
    response: Comment,
    external: Comment,
    external_comments: list[Comment],
    until: datetime,
    *,
    allow_unambiguous_mention: bool,
) -> bool:
    if exact_comment_reference(response, external):
        return True
    external_root = review_thread_root(external)
    if external_root is not None and review_thread_root(response) == external_root:
        eligible = [
            candidate
            for candidate in external_comments
            if review_thread_root(candidate) == external_root
            and response_is_after(response, candidate, until)
        ]
        return len(eligible) == 1 and eligible[0].key == external.key
    if allow_unambiguous_mention and mentions_login(response.body, external.author):
        # A mention after the author's later comments answers those comments;
        # the opening post never makes them ambiguous.
        eligible = [
            candidate
            for candidate in external_comments
            if normalize_login(candidate.author) == normalize_login(external.author)
            and (candidate.kind != OPENING_POST or external.kind == OPENING_POST)
            and response_is_after(response, candidate, until)
        ]
        return len(eligible) == 1 and eligible[0].key == external.key
    return False


def is_director_record(comment: Comment) -> bool:
    prefixes = director_questions.QUESTION_PREFIXES + director_questions.DECISION_PREFIXES
    return comment.kind == "issue_comment" and comment.body.strip().lstrip("*").startswith(prefixes)


def is_bookkeeping(comment: Comment) -> bool:
    """Director questions, decisions and planning records are awareness or state, not replies."""
    return is_director_record(comment) or PLAN_RECORD_MARKER in comment.body


def targeted_response_for(
    external: Comment,
    comments: list[Comment],
    external_comments: list[Comment],
    actor_logins: set[str],
    until: datetime,
    *,
    allow_unambiguous_mention: bool = False,
) -> Comment | None:
    responses = sorted(comments, key=lambda comment: (comment_activity_through(comment, until), comment.comment_id))
    for response in responses:
        if is_bookkeeping(response):
            continue
        if normalize_login(response.author) not in actor_logins or not response.body.strip():
            continue
        if not response_is_after(response, external, until):
            continue
        if response_targets(
            response,
            external,
            external_comments,
            until,
            allow_unambiguous_mention=allow_unambiguous_mention,
        ):
            return response
    return None


def later_reply_for(
    external: Comment,
    comments: list[Comment],
    actor_logins: set[str],
    until: datetime,
) -> Comment | None:
    """First owner or automation reply after the comment's latest version, targeted or not.

    Planning records and Director questions or decisions are bookkeeping, not replies.
    """
    responses = sorted(comments, key=lambda comment: (comment_activity_through(comment, until), comment.comment_id))
    for response in responses:
        if (
            is_bookkeeping(response)
            or normalize_login(response.author) not in actor_logins
            or not response.body.strip()
        ):
            continue
        if comment_timestamp(response.created_at) <= until and response_is_after(response, external, until):
            return response
    return None


def director_awareness_for(
    external: Comment,
    comments: list[Comment],
    trusted_logins: set[str],
    until: datetime,
) -> Comment | None:
    """Question/decision records cover links, or the preceding interval for 'above'."""
    records = sorted(
        (
            comment for comment in comments
            if is_director_record(comment)
            and normalize_login(comment.author) in trusted_logins
            and comment_timestamp(comment.created_at) <= until
        ),
        key=event_sort_key,
    )
    previous = None
    for record in records:
        if response_is_after(record, external, until):
            if exact_comment_reference(record, external):
                return record
            # A linked record has an explicit scope; 'above' must not broaden it.
            has_link = re.search(r"(?:issuecomment-|discussion_r|pullrequestreview-|#issue-)\d+", record.body) or any(
                candidate.kind == OPENING_POST and exact_comment_reference(record, candidate)
                for candidate in comments
            )
            in_interval = previous is None or event_sort_key(external) > event_sort_key(previous)
            if not has_link and re.search(r"\babove\b", record.body, re.IGNORECASE) and in_interval:
                return record
        previous = record
    return None


def owner_reaction_for(
    external: Comment,
    reactions: list[Reaction],
    self_logins: set[str],
    until: datetime,
) -> Reaction | None:
    candidates = [
        reaction
        for reaction in reactions
        if normalize_login(reaction.actor) in self_logins and reaction_is_after(reaction, external, until)
    ]
    return min(candidates, key=lambda reaction: reaction.created_at) if candidates else None


def compact_body(body: str, limit: int = 240) -> str:
    compact = re.sub(r"\s+", " ", body).strip()
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


def comment_states_for_thread(
    thread: dict[str, Any],
    comments: list[Comment],
    candidate_ids: set[tuple[str, int]],
    self_logins: set[str],
    bot_logins: set[str],
    reactions_by_comment: dict[tuple[str, int], list[Reaction] | None],
    until: datetime | None = None,
    automation_logins: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    effective_until = until or datetime.max.replace(tzinfo=timezone.utc)
    owner_logins = self_logins - bot_logins
    is_pull_request = isinstance(thread.get("pull_request"), dict)
    thread_kind = "pull_request" if is_pull_request else "issue"
    external_comments = [
        comment
        for comment in comments
        if is_external_comment(comment, owner_logins, bot_logins)
    ]
    results = []
    for comment in sorted(external_comments, key=event_sort_key):
        if comment.key not in candidate_ids:
            continue
        owner_response = targeted_response_for(
            comment,
            comments,
            external_comments,
            owner_logins,
            effective_until,
        )
        bot_response = targeted_response_for(
            comment,
            comments,
            external_comments,
            bot_logins,
            effective_until,
            allow_unambiguous_mention=True,
        )
        reaction_coverage_known = comment.key in reactions_by_comment
        owner_reaction = owner_reaction_for(
            comment,
            reactions_by_comment.get(comment.key) or [],
            owner_logins,
            effective_until,
        )
        awareness = director_awareness_for(comment, comments, owner_logins | bot_logins, effective_until)
        later_reply = later_reply_for(comment, comments, owner_logins | bot_logins, effective_until)
        if owner_response is not None:
            owner_seen: bool | None = True
            owner_seen_evidence = {
                "kind": "reply",
                "actor": owner_response.author,
                "url": owner_response.url,
                "created_at": owner_response.created_at,
            }
        elif owner_reaction is not None:
            owner_seen = True
            owner_seen_evidence = {
                "kind": "reaction",
                "actor": owner_reaction.actor,
                "content": owner_reaction.content,
                "created_at": owner_reaction.created_at,
            }
        elif awareness is not None:
            owner_seen = True
            owner_seen_evidence = {
                "kind": "director_record",
                "actor": awareness.author,
                "url": awareness.url,
                "created_at": awareness.created_at,
            }
        elif reaction_coverage_known:
            owner_seen = False
            owner_seen_evidence = None
        else:
            owner_seen = None
            owner_seen_evidence = None

        public_response = owner_response or bot_response
        publicly_addressed = public_response is not None
        public_response_actor = "owner" if owner_response is not None else "bot" if bot_response is not None else None
        closed_at = rollup.parse_timestamp(str(thread.get("closed_at") or ""))
        closed_after = closed_at is not None and comment_activity_through(comment, effective_until) < closed_at <= effective_until
        handled_closed = thread.get("state") == "closed" and (publicly_addressed or closed_after)
        if handled_closed:
            attention_state = "handled"
        elif owner_seen is None:
            attention_state = "coverage_unknown"
        elif not owner_seen and public_response_actor == "bot":
            attention_state = "bot_answered_needs_your_eyes"
        elif not owner_seen:
            attention_state = "needs_your_eyes"
        elif not publicly_addressed:
            attention_state = "seen_unanswered"
        else:
            attention_state = "handled"
        author_kind, automation_for = author_class(comment.author, automation_logins or {})

        results.append(
            {
                "repo": comment.repo,
                "number": comment.number,
                "thread_kind": thread_kind,
                "title": str(thread.get("title") or ""),
                "state": str(thread.get("state") or ""),
                "thread_url": str(thread.get("html_url") or ""),
                "comment_kind": comment.kind,
                "comment_id": comment.comment_id,
                "comment_url": comment.url,
                "author": comment.author,
                "author_class": author_kind,
                "automation_for": automation_for,
                "created_at": comment.created_at,
                "updated_at": comment.updated_at,
                "body_excerpt": compact_body(comment.body),
                "owner_seen": owner_seen,
                "owner_seen_evidence": owner_seen_evidence,
                "publicly_addressed": publicly_addressed,
                "public_response_actor": public_response_actor,
                "public_response_url": public_response.url if public_response is not None else None,
                "later_reply_url": later_reply.url if later_reply is not None else None,
                "attention_state": attention_state,
                "handled_reason": "closed_after_comment" if handled_closed and closed_after else "closed_with_response" if handled_closed else None,
            }
        )
    return results


def names_director(text: str, terms: list[str], logins: set[str] | frozenset[str] = frozenset()) -> bool:
    def pattern(term: str) -> str:
        name = re.escape(term.removeprefix("@"))
        if normalize_login(term) in logins:
            # A login inside OWNER/REPO or a URL names a repository, not the Director.
            return rf"(?<![\w/-])@?{name}(?![\w/-])"
        return rf"(?<![\w-])@?{name}(?![\w-])"

    return any(re.search(pattern(term), text, flags=re.IGNORECASE) for term in terms if term.strip())


def current_director_wait(
    reason: str, terms: list[str], logins: set[str] | frozenset[str] = frozenset()
) -> str | None:
    """Classify a recorded wait that is on the Director now.

    Only the first step of a sequence is current ("Mike's testing, then Chris"),
    and negated clauses ("no Director decision") name nobody. Wording of a hold
    the Director imposed ("Chris to resume ...", "Chris selects it") makes it a
    ``director_hold``: still listed, since that wording can also describe a real
    pending decision, but not attention on its own. Anything else is a
    ``decision``.
    """
    current = re.split(
        r"(?:[,;]\s*|\s+)(?:then|thereafter|followed by|after which)\b", reason, maxsplit=1, flags=re.IGNORECASE
    )[0]
    asserted = re.sub(r"\b(?:no|not|nothing|without|never)\b[^.;]*", "", current, flags=re.IGNORECASE)
    if not names_director(asserted, terms, logins):
        return None
    if re.search(r"\b(?:resum\w*|park\w*|holds?|select\w*|reprioriti\w*|revisit\w*)\b", asserted, re.IGNORECASE):
        return "director_hold"
    return "decision"


def director_wait_reason(
    repo: str, issue: dict[str, Any], terms: list[str], logins: set[str] | frozenset[str] = frozenset(),
    wait: dict[str, Any] | None = None,
) -> tuple[str, str] | None:
    """Return the recorded wait on the Director now and its kind, preferring a decision.

    ``wait`` is the shared resolver's trusted clear or waiting record. Without
    one, the Current Status prose is read and marked for review.
    """
    body = str(issue.get("body") or "")
    sections = github_direction_next.section_map(body)
    status = next((text for title, text in sections.items() if title.casefold() == "current status"), body)
    record: dict[str, Any] = {"repo": repo, "number": issue.get("number"), "url": str(issue.get("html_url") or ""),
                              **({"structured_wait": wait} if wait else {"wait_review_required": True})}
    found: tuple[str, str] | None = None
    for wait in github_direction_next.waiting_records(record, status):
        kind = None if wait["non_external"] else current_director_wait(wait["waiting_for"], terms, logins)
        if kind == "decision":
            return str(wait["waiting_for"]), kind
        if kind and found is None:
            found = str(wait["waiting_for"]), kind
    return found


def question_gap(comments: list[Comment], director_logins: set[str], decision_authors: set[str]) -> str | None:
    """Say why a wait on the Director has no open question, or None when one is open."""
    director = "director"
    discussion = [
        {
            "body": comment.body,
            "author": director if normalize_login(comment.author) in director_logins else normalize_login(comment.author),
            "url": comment.url,
            "id": comment.comment_id,
        }
        for comment in sorted(comments, key=event_sort_key)
        if comment.kind == "issue_comment"
    ]
    asked = director_questions.questions(discussion, director, decision_authors)
    if not asked:
        return "no_question"
    if all(question["status"] == "answered" for question in asked):
        return "questions_answered"
    return None


def structured_wait_issues(
    repositories: list[str], include_archived: bool = False,
) -> tuple[dict[str, dict[int, dict[str, Any]]], list[dict[str, str]]]:
    """Find open issues whose comments carry a structured wait, whatever their labels.

    One search per account, kept to the scanned repositories. Search
    limits or failures are reported as partial coverage of the waits lane.
    """
    scanned = {repo.casefold(): repo for repo in repositories if "/" in repo}
    found: dict[str, dict[int, dict[str, Any]]] = {}
    errors: list[dict[str, str]] = []
    for owner in sorted({repo.split("/", 1)[0] for repo in scanned.values()}, key=str.casefold):
        query = repository_scope.search_query(
            f'user:{owner} is:issue is:open in:comments "waiting_for"', include_archived=include_archived
        )
        # Count distinct issues, not entries, and require GitHub to say each page
        # is complete, as director_waits.collect_query does: a duplicate, a
        # changing total or a missing incomplete_results can hide a candidate.
        seen: set[tuple[str, int]] = set()
        totals: list[int] = []
        problems: list[str] = []
        try:
            for page in range(1, 11):
                payload = run_json([GH, "api", "search/issues?" + urlencode({"q": query, "per_page": 100, "page": page})])
                total = payload.get("total_count") if isinstance(payload, dict) else None
                if (
                    not isinstance(payload, dict) or not isinstance(payload.get("items"), list)
                    or isinstance(total, bool) or not isinstance(total, int) or total < 0
                ):
                    raise rollup.RollupError("Invalid structured wait search response.")
                totals.append(total)
                if payload.get("incomplete_results") is not False:
                    problems.append("GitHub did not report complete results")
                entries = payload["items"]
                for item in entries:
                    found_repo = rollup.repo_from_search_url(item.get("repository_url")) if isinstance(item, dict) else None
                    if not found_repo or not isinstance(item.get("number"), int) or isinstance(item["number"], bool):
                        problems.append("a result had no issue identity")
                        continue
                    key = (str(found_repo).casefold(), item["number"])
                    if key in seen:
                        problems.append("an issue repeated during pagination")
                        continue
                    seen.add(key)
                    repo = scanned.get(key[0])
                    if repo and "pull_request" not in item:
                        found.setdefault(repo, {})[item["number"]] = item
                if len(entries) < 100 or len(seen) >= total:
                    break
            if len(set(totals)) > 1:
                problems.append("the result total changed during pagination")
            if totals and len(seen) < max(totals):
                problems.append("the search limit or pagination left results unread")
        except rollup.RollupError as exc:
            errors.append({"repo": owner, "lane": "director_waits_search", "error": rollup.trim(str(exc))})
        if problems:
            errors.append({"repo": owner, "lane": "director_waits_search",
                           "error": "Structured wait search coverage is incomplete: "
                                    + "; ".join(dict.fromkeys(problems)) + "."})
    return found, errors


def unasked_director_waits(
    repo: str,
    terms: list[str],
    director_logins: set[str],
    decision_authors: set[str],
    structured: dict[int, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Waits on the Director with no open question, resolved from each complete discussion.

    Candidates are open plan:waiting issues plus ``structured``: open issues
    found with a structured wait in their comments, whatever their labels.
    """
    query = urlencode({"state": "open", "labels": WAITING_LABEL, "per_page": 100})
    try:
        issues = api_list(f"repos/{repo}/issues?{query}")
    except rollup.RollupError as exc:
        return [], [{"repo": repo, "lane": "director_waits", "error": rollup.trim(str(exc))}]
    candidates = {issue["number"]: issue for issue in issues if isinstance(issue.get("number"), int)}
    candidates.update({number: item for number, item in (structured or {}).items() if number not in candidates})
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for number, issue in sorted(candidates.items()):
        if "pull_request" in issue:
            continue
        # Body prose can be stale: a newer trusted comment may record or clear a
        # hold before search indexes it, so read every candidate's discussion.
        try:
            items = api_list(f"repos/{repo}/issues/{number}/comments?per_page=100")
        except rollup.RollupError as exc:
            errors.append({"repo": repo, "lane": f"director_waits/{number}", "error": rollup.trim(str(exc))})
            continue
        unresolved = False
        try:
            record, recorded = github_plan_wait.resolve(issue, items, github_plan_wait.authors(repo))
            # A trusted record that asks for review is an unknown wait, not prose.
            unresolved = recorded and record["state"] == "review"
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            # An unreadable record may hide or release a hold. Keep the issue as
            # an unknown wait for review, never resolved from prose.
            errors.append({"repo": repo, "lane": f"director_waits/{number}",
                           "error": rollup.trim(f"Structured wait record needs review: {exc}")})
            record, unresolved = None, True
        wait_record = record if record and record["state"] != "review" else None
        wait = (("unknown: the structured wait record needs review", "unresolved") if unresolved
                else director_wait_reason(repo, issue, terms, director_logins, wait_record))
        if wait is None:
            continue
        waiting_for, wait_kind = wait
        comments = [comment for item in items if (comment := normalize_issue_comment(repo, item)) is not None]
        gap = question_gap(comments, director_logins, decision_authors)
        if gap is None and not unresolved:
            continue
        rows.append(
            {
                "repo": repo,
                "number": number,
                "title": str(issue.get("title") or ""),
                "url": str(issue.get("html_url") or ""),
                "waiting_for": compact_body(waiting_for),
                "wait_kind": wait_kind,
                "question_gap": gap,
                "updated_at": str(issue.get("updated_at") or ""),
                "wait_source": ("unresolved_structured_wait" if unresolved
                                else "structured_wait" if wait_record else "legacy_wait_review"),
                "wait_review_required": wait_record is None,
            }
        )
    return rows, errors


def merge_candidate_ids(
    target: dict[tuple[str, int], set[tuple[str, int]]],
    source: dict[tuple[str, int], set[tuple[str, int]]],
) -> None:
    for thread_key, comment_ids in source.items():
        target.setdefault(thread_key, set()).update(comment_ids)


def outbound_threads(
    self_logins: set[str], settings: dict[str, Any],
) -> tuple[list[tuple[str, int]], list[dict[str, str]]]:
    """Search participation, then verify it from full thread activity."""
    threads: list[tuple[str, int]] = []
    errors: list[dict[str, str]] = []
    if not self_logins:
        return [], [{"repo": "-", "lane": "outbound_search", "error": "Outbound scanning requires a human --self-login or configured subject."}]
    for login in sorted(self_logins):
        query = repository_scope.search_query(
            f"involves:{login} " + " ".join(f"-user:{owner}" for owner in sorted(self_logins))
            + f" updated:>={format_api_timestamp(settings['since'])}",
            include_archived=settings.get("include_archived", False),
        )
        fetched = 0
        try:
            for page in range(1, 11):
                endpoint = "search/issues?" + urlencode({"q": query, "per_page": 100, "page": page})
                payload = run_json([GH, "api", endpoint])
                if (
                    not isinstance(payload, dict) or not isinstance(payload.get("items"), list)
                    or not isinstance(payload.get("total_count"), int)
                ):
                    raise rollup.RollupError("Invalid outbound search response.")
                entries = payload["items"]
                fetched += len(entries)
                for item in entries:
                    repo = rollup.repo_from_search_url(item.get("repository_url"))
                    number = item.get("number")
                    if not repo or not isinstance(number, int):
                        raise rollup.RollupError("Outbound search returned a thread without repository/number.")
                    if normalize_login(repo.split("/", 1)[0]) not in self_logins:
                        threads.append((repo, number))
                if payload.get("incomplete_results") or (fetched < payload["total_count"] and (len(entries) < 100 or page == 10)):
                    errors.append({"repo": login, "lane": "outbound_search", "error": "Outbound search coverage is incomplete (GitHub search limit or partial results)."})
                if len(entries) < 100 or fetched >= payload["total_count"]:
                    break
        except rollup.RollupError as exc:
            errors.append({"repo": login, "lane": "outbound_search", "error": rollup.trim(str(exc))})
    return unique_threads(threads), errors


def outbound_mentions(
    outbound_self: set[str], recorders: set[str], settings: dict[str, Any],
    excluded_repositories: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Director mentions outside the scan, searched under the Director's own login once approved."""
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    def read_comment(repo: str, comment_id: int) -> Any:
        return run_json([GH, "api", f"repos/{repo}/issues/comments/{comment_id}"])
    def read_later(issue_url: str, since: str) -> list[dict[str, Any]]:
        return api_list(f"{director_mentions.thread_comments_path(issue_url)}?{urlencode({'since': since})}")
    for login in sorted(outbound_self) or [None]:
        mentions, gap, error = director_mentions.collect(
            login, format_api_timestamp(settings["since"]), settings.get("mention_approval"), recorders,
            read_comment, read_later, until=format_api_timestamp(settings["until"]),
            excluded_repositories=excluded_repositories, excluded_owners=outbound_self,
        )
        if gap or error:
            errors.append({"repo": login or "-", "lane": "director_mentions", "error": f"Mentions outside these repositories are not covered: {gap or error}"})
        rows.extend(mentions)
    rows.sort(key=lambda row: row["updated_at"], reverse=True)
    return rows, errors


def outbound_states_for_thread(
    thread: dict[str, Any], comments: list[Comment], self_logins: set[str],
    bot_logins: set[str], settings: dict[str, Any], automation: dict[str, str],
    last_edits: dict[tuple[str, int, str, int], Edit],
) -> list[dict[str, Any]]:
    own_activity = []
    for comment in comments:
        if normalize_login(comment.author) not in self_logins:
            continue
        edit = last_edits.get(comment.global_key)
        # Maintainers can edit somebody else's posts. Only verified human edits
        # advance the cutoff; unknown editors leave the original activity time.
        own_activity.append(replace(
            comment, updated_at=edit.at if edit and normalize_login(edit.editor) in self_logins else comment.created_at,
        ))
    if not own_activity:
        return []  # Search also matches mentions and assignments.
    latest = max(comment_activity_through(comment, settings["until"]) for comment in own_activity)
    results = []
    for comment in sorted(comments, key=event_sort_key):
        if (
            not is_external_comment(comment, self_logins, bot_logins)
            or not comment_in_window(comment, settings["since"], settings["until"])
            or comment_activity_through(comment, settings["until"]) <= latest
        ):
            continue
        kind, automation_for = author_class(comment.author, automation)
        results.append({
            "repo": comment.repo, "number": comment.number,
            "thread_kind": "pull_request" if isinstance(thread.get("pull_request"), dict) else "issue",
            "title": str(thread.get("title") or ""), "state": str(thread.get("state") or ""),
            "thread_url": str(thread.get("html_url") or ""),
            "comment_kind": comment.kind, "comment_id": comment.comment_id, "comment_url": comment.url,
            "author": comment.author, "author_class": kind, "automation_for": automation_for,
            "created_at": comment.created_at, "updated_at": comment.updated_at,
            "body_excerpt": compact_body(comment.body), "last_self_activity_at": format_api_timestamp(latest),
            "attention_state": "outbound_reply", "owner_seen": None, "owner_seen_evidence": None,
            "publicly_addressed": False, "public_response_actor": None, "public_response_url": None,
            "handled_reason": None,
        })
    return results


def collect_payload(settings: dict[str, Any]) -> dict[str, Any]:
    self_logins = {normalize_login(login) for login in settings["self_logins"] if normalize_login(login)}
    bot_logins = {normalize_login(login) for login in settings["bot_logins"] if normalize_login(login)}
    outbound_self = {normalize_login(login) for login in settings.get("outbound_self_logins", []) if normalize_login(login)}
    auth_login = authenticated_login()
    normalized_auth = normalize_login(auth_login)
    if normalized_auth:
        if normalized_auth in bot_logins or is_github_bot(auth_login, ""):
            bot_logins.add(normalized_auth)
        elif not settings.get("director_login"):
            self_logins.add(normalized_auth)
            outbound_self.add(normalized_auth)

    repositories = resolve_repositories(settings)
    if not settings.get("director_login"):
        self_logins.update(normalize_login(repo.split("/", 1)[0]) for repo in repositories if "/" in repo)
    candidate_ids: dict[tuple[str, int], set[tuple[str, int]]] = {}
    coverage_errors: list[dict[str, str]] = []
    people = people_identities(self_logins - bot_logins, settings.get("people_index"))
    if people["status"] == "error":
        coverage_errors.append({"repo": "-", "lane": "people_index", "error": rollup.trim(people["error"])})
    self_logins.difference_update(bot_logins)
    outbound_self.difference_update(bot_logins)
    wait_terms = rollup.unique([*sorted(self_logins), *people["director_names"], *DIRECTOR_ROLE_WORDS])
    unasked_waits: list[dict[str, Any]] = []

    worker_count = min(MAX_WORKERS, max(1, len(repositories)))
    if settings["scan_repository_scope"]:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    repository_activity,
                    repo,
                    settings["since"],
                    settings["until"],
                    self_logins,
                    bot_logins,
                ): repo
                for repo in repositories
            }
            structured_waits, structured_errors = structured_wait_issues(
                repositories, settings.get("include_archived", False)
            )
            coverage_errors.extend(structured_errors)
            wait_futures = [
                executor.submit(
                    unasked_director_waits, repo, wait_terms, self_logins, bot_logins, structured_waits.get(repo, {})
                )
                for repo in repositories
            ]
            for future in as_completed(futures):
                repo_candidates, repo_errors = future.result()
                merge_candidate_ids(candidate_ids, repo_candidates)
                coverage_errors.extend(repo_errors)
            for future in as_completed(wait_futures):
                wait_rows, wait_errors = future.result()
                unasked_waits.extend(wait_rows)
                coverage_errors.extend(wait_errors)
        unasked_waits.sort(key=lambda row: (row["repo"], row["number"]))

    full_history_threads = set(settings["threads"])
    for thread_key in full_history_threads:
        candidate_ids.setdefault(thread_key, set())
    outbound_scope: set[tuple[str, int]] = set()
    mention_rows: list[dict[str, Any]] = []
    if settings.get("outbound"):
        found, outbound_errors = outbound_threads(outbound_self, settings)
        coverage_errors.extend(outbound_errors)
        inbound_repositories = {repo.casefold() for repo in repositories} if settings["scan_repository_scope"] else set()
        full_history_keys = {(repo.casefold(), number) for repo, number in full_history_threads}
        outbound_scope.update(
            (repo, number) for repo, number in found
            if (repo.casefold(), number) not in full_history_keys and repo.casefold() not in inbound_repositories
        )
        for thread_key in outbound_scope:
            candidate_ids.setdefault(thread_key, set())
        mention_rows, mention_errors = outbound_mentions(
            outbound_self, self_logins | bot_logins, settings,
            repositories if settings["scan_repository_scope"] else [],
        )
        coverage_errors.extend(mention_errors)
        repositories = rollup.unique([*repositories, *(repo for repo, _ in found)])

    collected_threads: list[tuple[dict[str, Any], list[Comment], set[tuple[str, int]]]] = []
    reaction_candidates: list[Comment] = []
    scanned_threads = 0
    thread_worker_count = min(MAX_WORKERS, max(1, len(candidate_ids)))
    with ThreadPoolExecutor(max_workers=thread_worker_count) as executor:
        futures = {
            executor.submit(collect_thread, repo, number): (repo, number, ids)
            for (repo, number), ids in candidate_ids.items()
        }
        for future in as_completed(futures):
            repo, number, ids = futures[future]
            thread, comments, thread_errors = future.result()
            coverage_errors.extend(thread_errors)
            if thread is None or thread_errors:
                continue
            scanned_threads += 1
            comments_through_until = [
                comment
                for comment in comments
                if comment_timestamp(comment.created_at) <= settings["until"]
            ]
            effective_ids = ids
            if (repo, number) in full_history_threads:
                effective_ids = {
                    comment.key
                    for comment in comments_through_until
                    if is_external_comment(comment, self_logins, bot_logins)
                }
            collected_threads.append((thread, comments_through_until, effective_ids))
            reaction_candidates.extend(
                comment
                for comment in comments_through_until
                if (
                    (repo, number) in outbound_scope and (
                        comment.kind in (OPENING_POST, "review") or normalize_login(comment.author) in outbound_self
                    )
                ) or (
                    (repo, number) not in outbound_scope and comment.key in effective_ids
                    and is_external_comment(comment, self_logins, bot_logins)
                )
            )

    reactions_by_global_key, last_edits_by_global_key, reaction_errors = collect_candidate_reactions(
        reaction_candidates
    )
    coverage_errors.extend(reaction_errors)
    comment_states: list[dict[str, Any]] = []
    # Full-history threads and outbound threads the Director took part in are
    # already reported by their own rules; mentions elsewhere are listed below.
    covered_threads = {(repo.casefold(), number) for repo, number in full_history_threads}
    for thread, comments, effective_ids in collected_threads:
        enriched_comments = [
            replace(comment, updated_at=last_edits_by_global_key[comment.global_key].at)
            if comment.global_key in last_edits_by_global_key
            and comment_timestamp(last_edits_by_global_key[comment.global_key].at) > comment_timestamp(comment.updated_at)
            else comment
            for comment in comments
        ]
        if comments and comments[0].thread_key in outbound_scope:
            if any(normalize_login(comment.author) in outbound_self for comment in comments):
                covered_threads.add((comments[0].repo.casefold(), comments[0].number))
            comment_states.extend(outbound_states_for_thread(
                thread, enriched_comments, outbound_self, bot_logins, settings, people["automation"], last_edits_by_global_key,
            ))
            continue
        reactions_by_comment = {
            comment.key: reactions_by_global_key.get(comment.global_key)
            for comment in enriched_comments
            if comment.key in effective_ids and comment.global_key in reactions_by_global_key
        }
        comment_states.extend(
            comment_states_for_thread(
                thread,
                enriched_comments,
                effective_ids,
                self_logins,
                bot_logins,
                reactions_by_comment,
                settings["until"],
                people["automation"],
            )
        )

    mention_rows = [
        row for row in mention_rows if (row["repository"].casefold(), row["number"]) not in covered_threads
    ]
    comment_states.sort(
        key=lambda item: (item.get("created_at") or "", item.get("comment_id") or 0),
        reverse=True,
    )
    attention = [item for item in comment_states if item["attention_state"] != "handled"]
    state_counts = {
        state: sum(1 for item in comment_states if item["attention_state"] == state)
        for state in (
            "needs_your_eyes",
            "bot_answered_needs_your_eyes",
            "seen_unanswered",
            "coverage_unknown",
            "handled",
            "outbound_reply",
        )
    }
    coverage_errors.sort(key=lambda item: (item.get("repo") or "", item.get("lane") or "", item.get("error") or ""))
    decisions = [row for row in unasked_waits if row["wait_kind"] == "decision"]
    status = "degraded" if coverage_errors else "attention" if attention or decisions or mention_rows else "clear"
    return {
        "ok": not coverage_errors,
        "schema_version": 3,
        "script_version": 3,
        "generated_at": format_api_timestamp(datetime.now(timezone.utc)),
        "status": status,
        "window": {
            "label": settings["window_label"],
            "since": format_api_timestamp(settings["since"]),
            "until": format_api_timestamp(settings["until"]),
        },
        "repositories": repositories,
        "outbound": bool(settings.get("outbound")),
        "excluded_archived_repositories": rollup.unique(settings.get("excluded_archived_repositories", [])),
        "self_logins": sorted(self_logins),
        "bot_logins": sorted(bot_logins),
        "coverage": {
            "repository_count": len(repositories),
            "candidate_thread_count": len(candidate_ids),
            "full_history_thread_count": len(full_history_threads),
            "scanned_thread_count": scanned_threads,
            "errors": coverage_errors,
        },
        "counts": {
            "external_comments": len(comment_states),
            "attention": len(attention),
            "handled_closed": sum(1 for item in comment_states if item.get("handled_reason")),
            **state_counts,
            "unasked_director_waits": len(decisions),
            "unasked_director_holds": sum(1 for row in unasked_waits if row["wait_kind"] == "director_hold"),
            "unresolved_director_waits": sum(1 for row in unasked_waits if row["wait_kind"] == "unresolved"),
            "director_mentions": len(mention_rows),
            **{
                f"author_{kind}": sum(1 for item in attention if item["author_class"] == kind)
                for kind in ("person", "automation", "possible_automation")
            },
        },
        "attention": attention,
        **({"handled": [item for item in comment_states if item["attention_state"] == "handled"]}
           if settings.get("include_handled") else {}),
        "unasked_director_waits": unasked_waits,
        "director_mentions": mention_rows,
        "people_index": {
            "status": people["status"],
            "director_named": bool(people["director_names"]),
            "director_ambiguous": people["director_ambiguous"],
            "automation_logins": len(people["automation"]),
        },
        "limitations": [
            "Repository scans cover issues and PRs opened in the window, issue/PR conversation comments and inline PR review comments; full-history --thread scans also cover the opening post and non-empty PR review bodies.",
            "An edit to an issue or PR description is caught only by a full-history --thread scan or when the thread is opened in the window.",
            "Only configured automation is excluded; people-index automation accounts (contacts.github.bot_usernames) are labeled with who they act for, and unlisted logins ending in bot or automation are marked possible_automation.",
            "Waits on the Director are open plan:waiting issues, and open issues whose comments GitHub search finds with a structured wait, whose current wait (the trusted structured record when one is readable, otherwise the Current Status prose marked for review) names the Director's login, a people-index name, or the Director role, with no Director or Owner question still open; an unreadable trusted record is listed as an unresolved wait for review, whether or not a question is open; negated mentions are left out, and waits worded as the Director's own hold (resume, park, hold, select, revisit) are listed as director_hold without raising attention; repository scans only.",
            "Any owner reaction after the current comment version proves personal acknowledgement; owner replies require an exact permalink or an inline-review thread with one eligible external comment.",
            "A targeted automation reply may also use an unambiguous single-author mention. Trusted Director question: or Director decision: records prove awareness of linked comments, or, when they say 'above' without a comment link, external comments preceding them since the previous such record in the thread; awareness alone is not a public response.",
            "Closed threads are handled when a comment has a targeted public response or closure followed its latest version by the scan end; this does not imply personal acknowledgement. Open threads retain separate awareness and response rules.",
            "Opt-in --outbound searches involvement in repositories not owned by the configured human logins, verifies authorship or comment participation, and lists external messages in the window after their latest human activity, including review bodies and inline comments. Only edits with a verified human editor advance that cutoff; automation and maintainer edits do not. Explicit thread and portfolio scopes retain their normal rules when outbound discovery overlaps them. GitHub search indexing, visibility and its 1,000-result cap limit discovery; detected partial results degrade coverage.",
            "With --outbound, threads outside the scanned repositories that mention the Director and were updated in the window are listed as untrusted links. That search runs read-only under the Director gh login and only with the standing approval linked by --mention-approval; without it, the gap is reported as degraded coverage.",
            "External comments outside the configured lookback window are not included.",
        ],
    }


def render_markdown(payload: dict[str, Any]) -> str:
    window = payload["window"]
    counts = payload["counts"]
    lines = [
        "# External GitHub Comment Radar",
        "",
        f"- Status: `{payload['status']}`",
        f"- Portfolio window: `{window['since']}` through `{window['until']}`",
        f"- Full-history threads: {payload['coverage']['full_history_thread_count']}",
        f"- Repositories scanned: {payload['coverage']['repository_count']}",
        f"- Messages needing attention: {counts['attention']}",
        f"- Handled messages: {counts['handled']}",
        f"- Handled messages on closed threads: {counts.get('handled_closed', 0)}",
        f"- Waits on you with no open question: {counts.get('unasked_director_waits', 0)}"
        f" (plus {counts.get('unasked_director_holds', 0)} worded as a hold"
        f" and {counts.get('unresolved_director_waits', 0)} unknown pending review)",
    ]
    excluded_archived = payload.get("excluded_archived_repositories") or []
    if excluded_archived:
        lines.append(f"- Archived repositories excluded: {', '.join(excluded_archived)}; use --include-archived to include them.")
    sections = (
        ("needs_your_eyes", "Needs Your Eyes"),
        ("bot_answered_needs_your_eyes", "Bot Answered — Needs Your Eyes"),
        ("seen_unanswered", "Seen — No Public Response"),
        ("coverage_unknown", "Acknowledgement Coverage Unknown"),
        ("outbound_reply", "Outside Repositories — Replies After Your Last Activity"),
    )
    for state, heading in sections:
        items = [item for item in payload["attention"] if item["attention_state"] == state]
        if not items:
            continue
        lines.extend(["", f"## {heading}"])
        for item in items:
            label = f"{item['repo']}#{item['number']}"
            title = item["title"] or label
            lines.append("")
            author = f"`@{item['author']}`"
            if item.get("author_class") == "automation":
                author += f" (automation for {item.get('automation_for') or 'another person'})"
            elif item.get("author_class") == "possible_automation":
                author += " (possibly automation; not in the people index)"
            opened = item.get("comment_kind") == OPENING_POST
            lines.append(f"- [{label}: {title}]({item['thread_url']}) — {author} {'opened it' if opened else 'commented'} at `{item['created_at']}`")
            if not opened:
                lines.append(f"  - [Open comment]({item['comment_url']})")
            if item["public_response_actor"] == "bot":
                lines.append(f"  - [Bot response]({item['public_response_url']})")
            evidence = item.get("owner_seen_evidence")
            if isinstance(evidence, dict) and evidence.get("kind") == "reaction":
                lines.append(f"  - Owner reaction: `{evidence.get('content') or 'reaction'}`")
            if item["body_excerpt"]:
                lines.append(f"  - {item['body_excerpt']}")
    waits = payload.get("unasked_director_waits") or []
    for kind, heading in (
        ("decision", "Waiting On You — No Question Asked"),
        ("director_hold", "Waiting On You, Worded As A Hold — No Question Asked"),
        ("unresolved", "Wait Unknown — Structured Record Needs Review"),
    ):
        rows = [wait for wait in waits if wait.get("wait_kind", "decision") == kind]
        if not rows:
            continue
        lines.extend(["", f"## {heading}"])
        for wait in rows:
            reason = {"no_question": "no question was posted", "questions_answered": "every question is answered"}.get(
                wait["question_gap"], "a question is open")
            lines.append("")
            lines.append(f"- [{wait['repo']}#{wait['number']}: {wait['title']}]({wait['url']}) — {reason}")
            lines.append(f"  - Waiting for: {wait['waiting_for']}")
    mentions = payload.get("director_mentions") or []
    if mentions:
        lines.extend(["", "## Outside Repositories — Mentions Of You"])
        for mention in mentions:
            lines.append("")
            lines.append(f"- [{mention['repository']}#{mention['number']}]({mention['url']}) — thread updated at `{mention['updated_at']}`")
    people = payload.get("people_index") or {}
    if people and not people.get("director_named"):
        detail = (
            "several people in the people index match the scanned owners"
            if people.get("director_ambiguous")
            else "the people index does not name the Director"
        )
        lines.extend(["", f"Waits that name the Director only by name were not matched: {detail}."])
    if not payload["attention"] and not waits and not mentions:
        message = (
            "No surfaced comments require attention, but coverage is incomplete; this is not an all-clear."
            if payload["status"] == "degraded"
            else "No external comments need attention in the scanned window."
        )
        lines.extend(["", message])

    errors = payload["coverage"]["errors"]
    if errors:
        lines.extend(["", "## Coverage Gaps"])
        for error in errors:
            lines.append(f"- `{error['repo']}` / `{error['lane']}`: {error['error']}")
        lines.append("")
        lines.append("Coverage was incomplete, so this report is not an all-clear.")
    return "\n".join(lines).rstrip() + "\n"


def render_failure(error: str, fmt: str) -> str:
    payload = {
        "ok": False,
        "schema_version": 3,
        "script_version": 3,
        "generated_at": format_api_timestamp(datetime.now(timezone.utc)),
        "status": "degraded",
        "error": error,
    }
    if fmt == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return f"# External GitHub Comment Radar\n\n- Status: `degraded`\n- Error: {error}\n"


def main() -> int:
    args = parse_args()
    try:
        config = rollup.load_config(args.config)
        settings = resolve_settings(args, config)
        payload = collect_payload(settings)
        rendered = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            if args.format == "json"
            else render_markdown(payload)
        )
    except (OSError, rollup.RollupError, ValueError) as exc:
        rendered = render_failure(rollup.trim(str(exc)), args.format)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        return EXIT_DEGRADED

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    if payload["status"] == "degraded":
        return EXIT_DEGRADED
    if payload["status"] == "attention":
        return EXIT_ATTENTION
    return EXIT_CLEAR


if __name__ == "__main__":
    raise SystemExit(main())
