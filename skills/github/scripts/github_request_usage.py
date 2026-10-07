#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Private per-caller HTTP receipts and shared polling budget, without bodies."""

from __future__ import annotations

import argparse
import collections
import fcntl
import hashlib
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse
from typing import Any

import github_identity


def state_dir() -> pathlib.Path:
    override = os.environ.get("GITHUB_RETRY_STATE_DIR")
    runtime = os.environ.get("CODE_HOME") or os.environ.get("CODEX_HOME")
    return pathlib.Path(override).expanduser() if override else (
        pathlib.Path(runtime).expanduser() if runtime else pathlib.Path.home() / ".code"
    ) / "state/github-retry"


def _integer(value: Any) -> int | None:
    try:
        number = int(value)
        return number if number >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _private_open(path: pathlib.Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "a+", encoding="utf-8")


def _budget_path(host: str, actor: str, bucket: str, repository: str | None) -> pathlib.Path:
    # One App installation per repository owner; the same App actor can serve
    # several installations. Unknown/default scope must not overwrite one.
    owner = repository.split("/", 1)[0].casefold() if repository else "configured-installation"
    digest = hashlib.sha256(f"{host}\0{actor.casefold()}\0{owner}\0{bucket}".encode()).hexdigest()
    return state_dir() / "request-usage" / f"budget-{digest}.json"


def record_response(
    *, method: str, path: str, status: int, headers: dict[str, str],
    operation: str, actor: str | None, host: str = "github.com", bucket: str = "rest_core",
    now: float | None = None,
) -> None:
    """Record each HTTP attempt once. Optional accounting never fails an API call.

    CLI commands without HTTP headers are deliberately outside this ledger;
    the report is a lower bound, not an installation-wide audit.
    """
    if status <= 0 or headers.get("x-codex-synthetic-response") == "app-actor":
        return
    timestamp = time.time() if now is None else now
    parts = urllib.parse.urlsplit(path).path.strip("/").split("/")
    repository = "/".join(parts[1:3]) if len(parts) >= 3 and parts[0] == "repos" else None
    if repository and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        repository = None
    resource = headers.get("x-ratelimit-resource")
    bucket = {"core": "rest_core", "graphql": "graphql", "search": "search"}.get(resource, bucket)
    if resource and resource not in {"core", "graphql", "search"}:
        bucket = resource if re.fullmatch(r"[a-z_]+", resource) else "unknown"
    quota = {name: _integer(headers.get(f"x-ratelimit-{name}")) for name in ("limit", "remaining", "reset", "used")}
    caller = pathlib.Path(os.environ.get("GITHUB_REQUEST_CALLER") or sys.argv[0]).name
    receipt = {
        "timestamp": timestamp, "helper": caller, "operation": operation,
        "repository": repository, "host": host, "actor": actor, "bucket": bucket,
        "method": method.upper(), "status": status,
        "request_id": headers.get("x-github-request-id"), "quota": quota,
        "primary_requests": None if bucket == "graphql" else int(
            status != 304 and parts != ["rate_limit"] and not (status in {403, 429} and quota["remaining"] == 0)
        ),
    }
    try:
        directory = state_dir() / "request-usage"
        with _private_open(directory / f"requests-{int(timestamp // 3600)}.jsonl") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.write(json.dumps(receipt, separators=(",", ":")) + "\n")
        if actor and quota["limit"] and quota["remaining"] is not None and quota["reset"]:
            with _private_open(_budget_path(host, actor, bucket, repository)) as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                stream.seek(0)
                try:
                    previous = json.load(stream)
                except (ValueError, TypeError):
                    previous = {}
                # Concurrent replies and 304s can contain older quota evidence.
                if isinstance(previous, dict) and previous.get("reset") == quota["reset"]:
                    remaining = _integer(previous.get("remaining"))
                    if remaining is not None:
                        quota["remaining"] = min(remaining, quota["remaining"])
                if not isinstance(previous, dict) or (_integer(previous.get("reset")) or 0) <= quota["reset"]:
                    stream.seek(0)
                    stream.truncate()
                    stream.write(json.dumps(quota))
    except (OSError, ValueError):
        pass


def quota_snapshot(*, host: str | None = None, actor: str | None = None, repository: str | None = None) -> dict[str, Any]:
    actor = actor or github_identity.automation_login()
    host = host or os.environ.get("GH_HOST") or "github.com"
    if not actor:
        return {}
    try:
        with _budget_path(host, actor, "rest_core", repository).open(encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
            quota = json.load(stream)
        return quota if isinstance(quota, dict) else {}
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {}


def polling_floor(*, host: str | None = None, actor: str | None = None, repository: str | None = None, now: float | None = None) -> float:
    """Reserve the final fifth of core quota for useful reads and writes."""
    quota = quota_snapshot(host=host, actor=actor, repository=repository)
    limit, remaining, reset = (_integer(quota.get(key)) for key in ("limit", "remaining", "reset"))
    timestamp = time.time() if now is None else now
    if limit and remaining is not None and reset and reset > timestamp and remaining <= limit * 0.2:
        return 300.0
    return 0.0


def report(*, since: float, now: float | None = None, actor: str | None = None) -> dict[str, Any]:
    timestamp = time.time() if now is None else now
    tallies: dict[tuple[str, ...], collections.Counter] = collections.defaultdict(collections.Counter)
    unreadable = 0
    for hour in range(int(since // 3600), int(timestamp // 3600) + 1):
        path = state_dir() / "request-usage" / f"requests-{hour}.jsonl"
        try:
            with path.open(encoding="utf-8") as stream:
                fcntl.flock(stream, fcntl.LOCK_SH)
                for line in stream:
                    try:
                        item = json.loads(line)
                        if not since <= item["timestamp"] <= timestamp:
                            continue
                        if actor and str(item.get("actor") or "").casefold() != actor.casefold():
                            continue
                        key = tuple(str(item.get(field) or "unknown") for field in (
                            "helper", "operation", "repository", "host", "actor", "bucket"
                        ))
                        tally = tallies[key]
                        tally["http_requests"] += 1
                        tally["not_modified"] += item["status"] == 304
                        tally["primary_requests"] += item.get("primary_requests") or 0
                        tally["unknown_cost"] += item.get("primary_requests") is None
                    except (ValueError, TypeError, KeyError):
                        unreadable += 1
        except FileNotFoundError:
            continue
        except OSError:
            unreadable += 1
    consumers = [dict(zip(("helper", "operation", "repository", "host", "actor", "bucket"), key), **tally)
                 for key, tally in tallies.items()]
    consumers.sort(key=lambda item: (-item["primary_requests"], -item["http_requests"], item["helper"]))
    return {"since": since, "until": timestamp, "actor": actor, "coverage": "instrumented HTTP attempts only; lower bound",
            "unreadable_records": unreadable, "consumers": consumers}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=1.0)
    parser.add_argument("--actor", default=github_identity.automation_login(), help="Default: configured automation actor")
    args = parser.parse_args()
    if not 0 < args.hours <= 168:
        parser.error("--hours must be between 0 and 168")
    print(json.dumps(report(since=time.time() - args.hours * 3600, actor=args.actor), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
