#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Find external GitHub comments that have not received a later response."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import github_work_rollup as rollup


GH = os.environ.get("GITHUB_UNANSWERED_COMMENTS_GH") or rollup.GH
DEFAULT_CONFIG = ROOT / ".local/github-work-rollup.yaml"
DEFAULT_WINDOW = "30d"
DEFAULT_BOT_LOGINS = ("shiny-code-bot",)
MAX_WORKERS = 8
EXIT_CLEAR = 0
EXIT_ATTENTION = 2
EXIT_DEGRADED = 3


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
    in_reply_to_id: int | None = None

    @property
    def key(self) -> tuple[str, int]:
        return self.kind, self.comment_id

    @property
    def thread_key(self) -> tuple[str, int]:
        return self.repo, self.number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report external issue and PR comments without a later response from the user or automation bot."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Optional GitHub work rollup YAML config.")
    parser.add_argument("--repo", action="append", default=[], help="OWNER/REPO. May be repeated.")
    parser.add_argument("--repo-owner", action="append", default=[], help="Owner whose non-archived repos should be scanned.")
    parser.add_argument("--thread", action="append", default=[], help="Full-history OWNER/REPO#NUMBER or GitHub issue/PR URL.")
    parser.add_argument("--self-login", action="append", default=[], help="Human login whose later comment counts as a response.")
    parser.add_argument("--bot-login", action="append", default=[], help="Automation login whose direct reply counts as a response.")
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
    bot_logins = rollup.unique(
        [*DEFAULT_BOT_LOGINS, *rollup.as_str_list(config.get("comment_bot_logins")), *args.bot_login]
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
        return []
    pages = payload if all(isinstance(page, list) for page in payload) else [payload]
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


def event_sort_key(comment: Comment) -> tuple[str, int]:
    return comment.created_at, comment.comment_id


def mentions_login(body: str, login: str) -> bool:
    escaped = re.escape(login.removeprefix("@"))
    return bool(re.search(rf"(?<![A-Za-z0-9-])@{escaped}(?![A-Za-z0-9-])", body, flags=re.IGNORECASE))


def bot_comment_responds(response: Comment, external: Comment) -> bool:
    if response.kind == "review_comment" and external.kind == "review_comment":
        response_root = response.in_reply_to_id or response.comment_id
        external_root = external.in_reply_to_id or external.comment_id
        if response_root == external_root:
            return True
    if mentions_login(response.body, external.author):
        return True
    if external.url and external.url in response.body:
        return True
    fragments = (f"issuecomment-{external.comment_id}", f"discussion_r{external.comment_id}")
    return any(fragment in response.body for fragment in fragments)


def response_for(
    external: Comment,
    comments: list[Comment],
    self_logins: set[str],
    bot_logins: set[str],
    until: datetime,
) -> Comment | None:
    external_created_at = comment_timestamp(external.created_at)
    external_updated_at = comment_timestamp(external.updated_at)
    external_activity_time = max(
        external_created_at,
        external_updated_at if external_updated_at <= until else external_created_at,
    )
    for response in sorted(comments, key=event_sort_key):
        response_time = comment_timestamp(response.created_at)
        if response_time < external_activity_time:
            continue
        if response_time == external_activity_time and response.comment_id <= external.comment_id:
            continue
        if not response.body.strip():
            continue
        author = normalize_login(response.author)
        if author in self_logins:
            return response
        if author in bot_logins and bot_comment_responds(response, external):
            return response
    return None


def compact_body(body: str, limit: int = 240) -> str:
    compact = re.sub(r"\s+", " ", body).strip()
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


def unanswered_for_thread(
    thread: dict[str, Any],
    comments: list[Comment],
    candidate_ids: set[tuple[str, int]],
    self_logins: set[str],
    bot_logins: set[str],
    until: datetime | None = None,
) -> list[dict[str, Any]]:
    effective_until = until or datetime.max.replace(tzinfo=timezone.utc)
    is_pull_request = isinstance(thread.get("pull_request"), dict)
    thread_kind = "pull_request" if is_pull_request else "issue"
    results = []
    for comment in sorted(comments, key=event_sort_key):
        if comment.key not in candidate_ids or not is_external_comment(comment, self_logins, bot_logins):
            continue
        if response_for(comment, comments, self_logins, bot_logins, effective_until) is not None:
            continue
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

    unanswered: list[dict[str, Any]] = []
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
            unanswered.extend(
                unanswered_for_thread(
                    thread,
                    comments_through_until,
                    effective_ids,
                    self_logins,
                    bot_logins,
                    settings["until"],
                )
            )

    unanswered.sort(key=lambda item: (item.get("created_at") or "", item.get("comment_id") or 0), reverse=True)
    coverage_errors.sort(key=lambda item: (item.get("repo") or "", item.get("lane") or "", item.get("error") or ""))
    status = "degraded" if coverage_errors else "attention" if unanswered else "clear"
    return {
        "ok": not coverage_errors,
        "schema_version": 1,
        "script_version": 1,
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
        "unanswered_count": len(unanswered),
        "unanswered": unanswered,
        "limitations": [
            "Repository scans cover issue/PR conversation comments and inline PR review comments; full-history --thread scans also cover non-empty PR review bodies.",
            "A human-account response counts when it is later in the same thread; an automation response must directly mention or reply to the external commenter.",
            "External comments outside the configured lookback window are not included.",
        ],
    }


def render_markdown(payload: dict[str, Any]) -> str:
    window = payload["window"]
    lines = [
        "# Unanswered GitHub Comments",
        "",
        f"- Status: `{payload['status']}`",
        f"- Portfolio window: `{window['since']}` through `{window['until']}`",
        f"- Full-history threads: {payload['coverage']['full_history_thread_count']}",
        f"- Repositories scanned: {payload['coverage']['repository_count']}",
        f"- Unanswered comments: {payload['unanswered_count']}",
    ]
    if payload["unanswered"]:
        lines.extend(["", "## Needs Response"])
        for item in payload["unanswered"]:
            label = f"{item['repo']}#{item['number']}"
            title = item["title"] or label
            lines.append("")
            lines.append(f"- [{label}: {title}]({item['thread_url']}) — `@{item['author']}` at `{item['created_at']}`")
            lines.append(f"  - [Open comment]({item['comment_url']})")
            if item["body_excerpt"]:
                lines.append(f"  - {item['body_excerpt']}")
    else:
        lines.extend(["", "No unanswered external comments were found in the scanned window."])

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
        "schema_version": 1,
        "script_version": 1,
        "generated_at": format_api_timestamp(datetime.now(timezone.utc)),
        "status": "degraded",
        "error": error,
    }
    if fmt == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return f"# Unanswered GitHub Comments\n\n- Status: `degraded`\n- Error: {error}\n"


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
