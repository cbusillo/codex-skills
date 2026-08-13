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
import github_identity


GH = os.environ.get("GITHUB_UNANSWERED_COMMENTS_GH") or rollup.GH
DEFAULT_CONFIG = ROOT / ".local/github-work-rollup.yaml"
DEFAULT_WINDOW = "30d"
DEFAULT_BOT_LOGINS = ()
MAX_WORKERS = 8
EXIT_CLEAR = 0
EXIT_ATTENTION = 2
EXIT_DEGRADED = 3
REACTIONS_QUERY = """
query($id: ID!, $after: String) {
  node(id: $id) {
    ... on Comment { lastEditedAt }
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report external issue and PR comments that still need the owner's attention."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Optional GitHub work rollup YAML config.")
    parser.add_argument("--repo", action="append", default=[], help="OWNER/REPO. May be repeated.")
    parser.add_argument("--repo-owner", action="append", default=[], help="Owner whose non-archived repos should be scanned.")
    parser.add_argument("--thread", action="append", default=[], help="Full-history OWNER/REPO#NUMBER or GitHub issue/PR URL.")
    parser.add_argument("--self-login", action="append", default=[], help="Human owner login whose reactions and replies count.")
    parser.add_argument("--bot-login", action="append", default=[], help="Automation login whose targeted replies count.")
    parser.add_argument("--window", help="Lookback such as 24h, 30d, or 12w.")
    parser.add_argument("--since", help="UTC ISO timestamp for the scan start.")
    parser.add_argument("--until", help="UTC ISO timestamp for the scan end.")
    parser.add_argument("--limit-repos", type=int, default=1000)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
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
    explicit_scope = bool(args.repo or args.repo_owner or threads)
    repositories = rollup.unique(args.repo if explicit_scope else rollup.as_str_list(config.get("repositories")))
    repo_owners = rollup.unique(args.repo_owner if explicit_scope else rollup.as_str_list(config.get("repo_owners")))
    configured_self = rollup.as_str_list(config.get("comment_self_logins"))
    if not configured_self:
        configured_self = rollup.as_str_list(config.get("subjects"))
    self_logins = rollup.unique([*configured_self, *args.self_login])
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
    inferred_self = [*repo_owners, *(repo.split("/", 1)[0] for repo in repositories if "/" in repo)]
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
        "explicit_scope": explicit_scope,
        "self_logins": self_logins,
        "bot_logins": bot_logins,
        "since": since.astimezone(timezone.utc),
        "until": until.astimezone(timezone.utc),
        "window_label": window_label,
        "limit_repos": args.limit_repos,
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
    repositories = [*settings["repositories"], *(repo for repo, _ in settings["threads"])]
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
            if isinstance(item, dict) and not item.get("isArchived") and item.get("nameWithOwner")
        ]
        if not visible_repositories:
            raise rollup.RollupError(f"Repository owner scope {owner!r} resolved to no visible non-archived repositories.")
        repositories.extend(
            visible_repositories
        )
    if not repositories:
        if settings["explicit_scope"]:
            raise rollup.RollupError("The explicit repository scope resolved to no repositories.")
        repositories.append(current_repository())
    return rollup.unique(repositories)


def normalize_login(value: object) -> str:
    return str(value or "").strip().casefold().removeprefix("@")


def is_github_bot(author: str, author_type: str) -> bool:
    login = normalize_login(author)
    return normalize_login(author_type) == "bot" or login.endswith("[bot]")


def is_external_comment(comment: Comment, self_logins: set[str], bot_logins: set[str]) -> bool:
    author = normalize_login(comment.author)
    if not author or not comment.body.strip():
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


def collect_comment_reactions(comment: Comment) -> tuple[list[Reaction], str]:
    if not comment.node_id:
        raise rollup.RollupError(f"Comment {comment.comment_id} has no GraphQL node id.")
    reactions: list[Reaction] = []
    last_edited_at = ""
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
            last_edited_at = node_last_edited_at
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
            return reactions, last_edited_at
        next_cursor = str(page_info.get("endCursor") or "")
        if not next_cursor or next_cursor in seen_cursors:
            raise rollup.RollupError(f"GitHub reaction pagination stalled for comment {comment.comment_id}.")
        seen_cursors.add(next_cursor)
        after = next_cursor


def collect_candidate_reactions(
    comments: list[Comment],
) -> tuple[
    dict[tuple[str, int, str, int], list[Reaction]],
    dict[tuple[str, int, str, int], str],
    list[dict[str, str]],
]:
    unique_comments = {comment.global_key: comment for comment in comments}
    reactions: dict[tuple[str, int, str, int], list[Reaction]] = {}
    last_edits: dict[tuple[str, int, str, int], str] = {}
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
                comment_reactions, last_edited_at = future.result()
                reactions[comment.global_key] = comment_reactions
                if last_edited_at:
                    last_edits[comment.global_key] = last_edited_at
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
        eligible = [
            candidate
            for candidate in external_comments
            if normalize_login(candidate.author) == normalize_login(external.author)
            and response_is_after(response, candidate, until)
        ]
        return len(eligible) == 1 and eligible[0].key == external.key
    return False


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
        elif reaction_coverage_known:
            owner_seen = False
            owner_seen_evidence = None
        else:
            owner_seen = None
            owner_seen_evidence = None

        public_response = owner_response or bot_response
        publicly_addressed = public_response is not None
        public_response_actor = "owner" if owner_response is not None else "bot" if bot_response is not None else None
        if owner_seen is None:
            attention_state = "coverage_unknown"
        elif not owner_seen and public_response_actor == "bot":
            attention_state = "bot_answered_needs_your_eyes"
        elif not owner_seen:
            attention_state = "needs_your_eyes"
        elif not publicly_addressed:
            attention_state = "seen_unanswered"
        else:
            attention_state = "handled"

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
                "created_at": comment.created_at,
                "updated_at": comment.updated_at,
                "body_excerpt": compact_body(comment.body),
                "owner_seen": owner_seen,
                "owner_seen_evidence": owner_seen_evidence,
                "publicly_addressed": publicly_addressed,
                "public_response_actor": public_response_actor,
                "public_response_url": public_response.url if public_response is not None else None,
                "attention_state": attention_state,
            }
        )
    return results


def merge_candidate_ids(
    target: dict[tuple[str, int], set[tuple[str, int]]],
    source: dict[tuple[str, int], set[tuple[str, int]]],
) -> None:
    for thread_key, comment_ids in source.items():
        target.setdefault(thread_key, set()).update(comment_ids)


def collect_payload(settings: dict[str, Any]) -> dict[str, Any]:
    self_logins = {normalize_login(login) for login in settings["self_logins"] if normalize_login(login)}
    bot_logins = {normalize_login(login) for login in settings["bot_logins"] if normalize_login(login)}
    auth_login = authenticated_login()
    normalized_auth = normalize_login(auth_login)
    if normalized_auth:
        if normalized_auth in bot_logins or is_github_bot(auth_login, ""):
            bot_logins.add(normalized_auth)
        else:
            self_logins.add(normalized_auth)

    repositories = resolve_repositories(settings)
    self_logins.update(normalize_login(repo.split("/", 1)[0]) for repo in repositories if "/" in repo)
    self_logins.difference_update(bot_logins)
    candidate_ids: dict[tuple[str, int], set[tuple[str, int]]] = {}
    coverage_errors: list[dict[str, str]] = []

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
            for future in as_completed(futures):
                repo_candidates, repo_errors = future.result()
                merge_candidate_ids(candidate_ids, repo_candidates)
                coverage_errors.extend(repo_errors)

    full_history_threads = set(settings["threads"])
    for thread_key in full_history_threads:
        candidate_ids.setdefault(thread_key, set())

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
                if comment.key in effective_ids and is_external_comment(comment, self_logins, bot_logins)
            )

    reactions_by_global_key, last_edits_by_global_key, reaction_errors = collect_candidate_reactions(
        reaction_candidates
    )
    coverage_errors.extend(reaction_errors)
    comment_states: list[dict[str, Any]] = []
    for thread, comments, effective_ids in collected_threads:
        enriched_comments = [
            replace(comment, updated_at=last_edits_by_global_key[comment.global_key])
            if comment.global_key in last_edits_by_global_key
            and comment_timestamp(last_edits_by_global_key[comment.global_key]) > comment_timestamp(comment.updated_at)
            else comment
            for comment in comments
        ]
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
            )
        )

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
        )
    }
    coverage_errors.sort(key=lambda item: (item.get("repo") or "", item.get("lane") or "", item.get("error") or ""))
    status = "degraded" if coverage_errors else "attention" if attention else "clear"
    return {
        "ok": not coverage_errors,
        "schema_version": 2,
        "script_version": 2,
        "generated_at": format_api_timestamp(datetime.now(timezone.utc)),
        "status": status,
        "window": {
            "label": settings["window_label"],
            "since": format_api_timestamp(settings["since"]),
            "until": format_api_timestamp(settings["until"]),
        },
        "repositories": repositories,
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
            **state_counts,
        },
        "attention": attention,
        "limitations": [
            "Repository scans cover issue/PR conversation comments and inline PR review comments; full-history --thread scans also cover non-empty PR review bodies.",
            "Any owner reaction after the current comment version proves personal acknowledgement; owner replies require an exact permalink or an inline-review thread with one eligible external comment.",
            "A targeted automation reply may also use an unambiguous single-author mention, but it never proves the owner saw the comment.",
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
        f"- Comments needing attention: {counts['attention']}",
        f"- Handled comments: {counts['handled']}",
    ]
    sections = (
        ("needs_your_eyes", "Needs Your Eyes"),
        ("bot_answered_needs_your_eyes", "Bot Answered — Needs Your Eyes"),
        ("seen_unanswered", "Seen — No Public Response"),
        ("coverage_unknown", "Acknowledgement Coverage Unknown"),
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
            lines.append(f"- [{label}: {title}]({item['thread_url']}) — `@{item['author']}` at `{item['created_at']}`")
            lines.append(f"  - [Open comment]({item['comment_url']})")
            if item["public_response_actor"] == "bot":
                lines.append(f"  - [Bot response]({item['public_response_url']})")
            evidence = item.get("owner_seen_evidence")
            if isinstance(evidence, dict) and evidence.get("kind") == "reaction":
                lines.append(f"  - Owner reaction: `{evidence.get('content') or 'reaction'}`")
            if item["body_excerpt"]:
                lines.append(f"  - {item['body_excerpt']}")
    if not payload["attention"]:
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
        "schema_version": 2,
        "script_version": 2,
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
