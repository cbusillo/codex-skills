#!/usr/bin/env -S uv run --no-cache --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Emit untrusted foreign-post notices; never start or widen work."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import math
from pathlib import Path
import re
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))
import director_mentions
from github_read import GitHubReader, GitHubReadError, GitHubReadShapeError, automation_only_gh_prefix_args
from github_review_markers import PRODUCT_REVIEW_MARKER

REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
MENTIONS = "director-mentions"  # watermark key; not a repository name
# Search indexing can lag; each mention search overlaps the previous one and
# skips thread versions already reported.
MENTION_OVERLAP = timedelta(minutes=15)
READ_ERRORS = (GitHubReadError, GitHubReadShapeError, OSError, ValueError, TypeError, KeyError)
# Enough of the comment to read it at a check; the full comment stays on GitHub.
EXCERPT_CHARS = 240


def timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("watermarks must be timestamps")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("watermarks must include a timezone")
    return parsed.astimezone(timezone.utc)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def excerpt(body: str) -> str:
    text = " ".join(body.split())
    return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS - 1].rstrip() + "…"


def read_comments(reader, repo: str, since: str) -> list[dict]:
    # GitHub's since filter is exclusive; overlap the boundary by one second.
    query_since = (timestamp(since) - timedelta(seconds=1)).isoformat()
    return reader.paged_json(
        f"repos/{repo}/issues/comments", step_prefix="foreign_comments",
        params={"since": query_since, "sort": "updated", "direction": "asc"},
        required_query_params={"since": query_since},
    )


def read_opened(reader, repo: str, since: str) -> list[dict]:
    query_since = (timestamp(since) - timedelta(seconds=1)).isoformat()
    issues = reader.paged_json(
        f"repos/{repo}/issues", step_prefix="foreign_opened",
        params={"since": query_since, "state": "all"}, required_query_params={"since": query_since},
    )
    return [{**issue, "body": f"{issue['title']}\n{issue.get('body') or ''}", "updated_at": issue["created_at"]}
            for issue in issues if issue["user"] is None or issue["user"]["type"] != "Bot"]


def installed_repositories(reader, owner: str) -> list[str]:
    repositories = reader.paged_json("/installation/repositories", collection_key="repositories",
                                     step_prefix="foreign_repositories")
    return [r["full_name"] for r in repositories
            if r["full_name"].split("/")[0].casefold() == owner.casefold() and r["archived"] is False]


def read_error(repo: str, error: Exception) -> dict:
    diagnostics = getattr(error, "diagnostics", {})
    return {"repository": repo, "error": "read incomplete",
            "retryable": isinstance(error, GitHubReadError) and bool(
                error.result.failure and error.result.failure.retryable),
            "retry_at": next((r.get("retryAt") for r in reversed(
                diagnostics.get("requests", [])
            ) if r.get("retryAt") is not None), None)}


def scan(repos, watermarks, own_logins, launchplane_logins, started, reader):
    """One since-scoped listing of each repository's comments and newly opened issues; failures retain its watermark."""
    notices, errors, next_watermarks = [], [], dict(watermarks)
    own = {login.casefold() for login in own_logins}
    launchplane = {login.casefold() for login in launchplane_logins}
    for repo in repos:
        since = watermarks[repo]
        pending = []
        try:
            for comment in read_comments(reader, repo, since) + read_opened(reader, repo, since):
                user = comment["user"]
                login = user["login"] if user is not None else None
                body = comment.get("body") or ""
                updated = comment["updated_at"]
                url = comment["html_url"]
                if (login is not None and not isinstance(login, str)) or not all(
                    isinstance(value, str) for value in (body, updated, url)
                ):
                    raise ValueError("comment metadata must be text")
                if timestamp(updated) < timestamp(since):
                    continue
                lines = body.splitlines()
                marker_seen = bool(lines and PRODUCT_REVIEW_MARKER.fullmatch(lines[0]))
                is_launchplane = (login or "").casefold() in launchplane or (
                    (login or "").casefold() in own and marker_seen)
                if is_launchplane or (login or "").casefold() not in own:
                    pending.append({
                        "repository": repo, "author": login, "url": url,
                        "updated_at": updated, "excerpt": excerpt(body),
                        "kind": "launchplane" if is_launchplane else "foreign",
                        "untrusted": True,
                        "review_marker": marker_seen,
                    })
        except READ_ERRORS as error:
            errors.append(read_error(repo, error))
            continue
        notices.extend(pending)
        next_watermarks[repo] = started
    return notices, errors, next_watermarks


def scan_mentions(args, repos, watermarks, seen, own_logins, started, reader):
    """Director mentions outside the watched repositories; a gap is reported, never skipped.

    Returns notices, coverage gaps, errors, watermarks and the reported thread
    versions ({url: updated_at}) still inside the overlap window.
    """
    since = (timestamp(watermarks[MENTIONS]) - MENTION_OVERLAP).isoformat().replace("+00:00", "Z")
    mentions, gap, error = director_mentions.collect(
        getattr(args, "director_login", None), since, getattr(args, "mention_approval", None),
        set(own_logins),
        lambda repo, comment_id: reader.get_json(f"repos/{repo}/issues/comments/{comment_id}",
                                                 step="foreign_mention_approval"),
        lambda issue_url, created: reader.paged_json(
            director_mentions.thread_comments_path(issue_url), step_prefix="foreign_mention_approval_thread",
            params={"since": created}, required_query_params={"since": created}),
        excluded_repositories=repos)
    if gap:  # Keep the watermark so a later approval catches up from here.
        return [], [{"lane": MENTIONS, "gap": f"mentions outside the watched list are not covered: {gap}"}], [], \
            watermarks, seen
    if error:
        return [], [], [{"repository": MENTIONS, "error": error, "retryable": True, "retry_at": None}], \
            watermarks, seen
    fresh = [mention for mention in mentions if seen.get(mention["url"]) != mention["updated_at"]]
    window = timestamp(since)
    kept = {url: updated for url, updated in {**seen, **{m["url"]: m["updated_at"] for m in mentions}}.items()
            if timestamp(updated) >= window}
    return fresh, [], [], {**watermarks, MENTIONS: started}, kept


def load_mention_seen(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text()) if path.exists() else {}
    seen = data.get("mention_seen", {}) if isinstance(data, dict) else None
    if not isinstance(seen, dict) or not all(isinstance(url, str) for url in seen):
        raise ValueError("mention_seen must be an object")
    for value in seen.values():
        timestamp(value)
    return dict(seen)


def load_state(path: Path, repos, initial: str, *, rewind=False) -> dict[str, str]:
    data = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("watch state must be an object")
    values = data.get("watermarks", {})
    if not isinstance(values, dict):
        raise ValueError("watermarks must be an object")
    for value in values.values():
        timestamp(value)
    values = dict(values)
    cover(values, repos, initial, rewind)
    return values


def cover(watermarks, repos, initial: str, rewind: bool) -> None:
    for repo in repos:
        watermarks.setdefault(repo, initial)
        if rewind and timestamp(initial) < timestamp(watermarks[repo]):
            watermarks[repo] = initial


def save_state(path: Path, watermarks, mention_seen=None):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(json.dumps({"watermarks": watermarks, "mention_seen": mention_seen or {}}) + "\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def watch_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_name(path.name + ".lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("another watcher owns this state file") from error
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", action="append",
                        help="default: the automation installation's non-archived repositories of --owner")
    parser.add_argument("--own-login", action="append", required=True)
    parser.add_argument("--launchplane-login", action="append", default=[])
    parser.add_argument("--director-login",
                        help="the Director's own login for mention searches outside the watched repositories")
    parser.add_argument("--mention-approval",
                        help="link to the Director's standing approval for those searches under their login")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--since")
    parser.add_argument("--interval", type=float, default=300)
    parser.add_argument("--deadline-seconds", type=float, default=1800)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--persistent", action="store_true",
                        help="keep watching notices and recover retryable reads within the deadline")
    args = parser.parse_args()
    if any(not math.isfinite(v) or v <= 0 for v in (args.interval, args.deadline_seconds)):
        parser.error("timings must be finite and positive")
    repos = list(dict.fromkeys(args.repo)) if args.repo else None
    if repos and any(not REPOSITORY.fullmatch(repo) or repo.split("/")[0].casefold() != args.owner.casefold()
           for repo in repos):
        parser.error("repositories must belong to the configured Director")
    deadline = time.monotonic() + args.deadline_seconds
    initial = args.since or utc_now()
    try:
        timestamp(initial)
        with watch_lock(args.state):
            return watch(args, repos, initial, deadline)
    except (OSError, ValueError, TypeError) as error:
        print(json.dumps({"error": str(error)}), flush=True)
        return 1


def watch(args, repos, initial, deadline):
    watermarks = load_state(args.state, [*(repos or []), MENTIONS], initial, rewind=args.since is not None)
    seen = load_mention_seen(args.state)
    covered = set(repos or [])
    while time.monotonic() < deadline:
        started = utc_now()
        reader = GitHubReader(operation="github.supervisor.foreign_watch", strict_actor=True,
                              gh_prefix_args=automation_only_gh_prefix_args(),
                              deadline_at=time.time() + max(0, deadline - time.monotonic()))
        try:
            watched = repos or installed_repositories(reader, args.owner)
        except READ_ERRORS as error:
            notices, errors, gaps = [], [read_error("installation", error)], []
        else:
            cover(watermarks, [repo for repo in watched if repo not in covered], initial, args.since is not None)
            covered.update(watched)
            notices, errors, watermarks = scan(watched, watermarks, args.own_login,
                                              args.launchplane_login, started, reader)
            mentions, gaps, mention_errors, watermarks, seen = scan_mentions(
                args, watched, watermarks, seen, args.own_login, started, reader)
            notices, errors = notices + mentions, errors + mention_errors
        print(json.dumps({"notices": notices, "errors": errors, "coverage_gaps": gaps,
                          "untrusted": True}), flush=True)
        save_state(args.state, watermarks, seen)
        persistent = getattr(args, "persistent", False)
        if args.once or (not persistent and (notices or errors)) or any(
            not error["retryable"] for error in errors
        ):
            return int(bool(errors))
        delay = max([args.interval] + [
            max(0, error["retry_at"] - time.time())
            for error in errors if isinstance(error["retry_at"], (int, float))
        ])
        # Carry a provider cooldown through the deadline so a service restart
        # cannot immediately read before the advertised retry time.
        time.sleep(delay if errors else min(delay, max(0, deadline - time.monotonic())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
