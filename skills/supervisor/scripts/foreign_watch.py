#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Emit untrusted foreign-post notices; never start or widen work."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))
from github_read import GitHubReader, GitHubReadError, GitHubReadShapeError

REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
REVIEW_MARKERS = ("launchplane:owner-review", "launchplane:product-review")


def timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("watermarks must be timestamps")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("watermarks must include a timezone")
    return parsed.astimezone(timezone.utc)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_comments(reader, repo: str, since: str) -> list[dict]:
    # GitHub's since filter is exclusive; overlap the boundary by one second.
    query_since = (timestamp(since) - timedelta(seconds=1)).isoformat()
    return reader.paged_json(
        f"repos/{repo}/issues/comments", step_prefix="foreign_comments",
        params={"since": query_since, "sort": "updated", "direction": "asc"},
        required_query_params={"since": query_since},
    )


def scan(repos, watermarks, own_logins, launchplane_logins, started, reader):
    """One since-scoped repository comment listing; failures retain its watermark."""
    notices, errors, next_watermarks = [], [], dict(watermarks)
    own = {login.casefold() for login in own_logins}
    launchplane = {login.casefold() for login in launchplane_logins}
    for repo in repos:
        since = watermarks[repo]
        pending = []
        try:
            for comment in read_comments(reader, repo, since):
                login = comment["user"]["login"]
                body = comment.get("body") or ""
                updated = comment["updated_at"]
                url = comment["html_url"]
                if not all(isinstance(value, str) for value in (login, body, updated, url)):
                    raise ValueError("comment metadata must be text")
                if timestamp(updated) < timestamp(since):
                    continue
                is_launchplane = login.casefold() in launchplane or any(
                    marker in body for marker in REVIEW_MARKERS
                )
                if is_launchplane or login.casefold() not in own:
                    pending.append({
                        "repository": repo, "author": login, "url": url,
                        "updated_at": updated,
                        "kind": "launchplane" if is_launchplane else "foreign",
                        "untrusted": True,
                    })
        except (GitHubReadError, GitHubReadShapeError, OSError, ValueError, TypeError, KeyError) as error:
            diagnostics = getattr(error, "diagnostics", {})
            errors.append({"repository": repo, "error": "comment read incomplete",
                           "retry_at": diagnostics.get("retry_at")})
            continue
        notices.extend(pending)
        next_watermarks[repo] = started
    return notices, errors, next_watermarks


def load_state(path: Path, repos, initial: str) -> dict[str, str]:
    data = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("watch state must be an object")
    values = data.get("watermarks", {})
    if not isinstance(values, dict):
        raise ValueError("watermarks must be an object")
    for value in values.values():
        timestamp(value)
    return {repo: values.get(repo, initial) for repo in repos}


def save_state(path: Path, watermarks):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps({"watermarks": watermarks}) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", action="append", required=True)
    parser.add_argument("--own-login", action="append", required=True)
    parser.add_argument("--launchplane-login", action="append", default=[])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--since")
    parser.add_argument("--interval", type=float, default=300)
    parser.add_argument("--deadline-seconds", type=float, default=1800)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if any(not math.isfinite(v) or v <= 0 for v in (args.interval, args.deadline_seconds)):
        parser.error("timings must be finite and positive")
    repos = list(dict.fromkeys(args.repo))
    if any(not REPOSITORY.fullmatch(repo) or repo.split("/")[0].casefold() != args.owner.casefold()
           for repo in repos):
        parser.error("repositories must belong to the configured Director")
    deadline = time.monotonic() + args.deadline_seconds
    initial = args.since or utc_now()
    try:
        timestamp(initial)
        watermarks = load_state(args.state, repos, initial)
        while time.monotonic() < deadline:
            started = utc_now()
            reader = GitHubReader(operation="github.read.issues", strict_actor=True,
                                  deadline_at=time.time() + max(0, deadline - time.monotonic()))
            notices, errors, watermarks = scan(repos, watermarks, args.own_login,
                                              args.launchplane_login, started, reader)
            print(json.dumps({"notices": notices, "errors": errors, "untrusted": True}), flush=True)
            save_state(args.state, watermarks)
            if notices or errors or args.once:
                return int(bool(errors))
            time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
    except (OSError, ValueError, TypeError) as error:
        print(json.dumps({"error": str(error)}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
