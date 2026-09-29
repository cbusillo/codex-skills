# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read-only fork dispositions for the shared cleanup policy's fork rule.

Reads GitHub through `gh`; never archives, deletes, or changes a repository.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

Runner = Callable[[list[str]], Any]
# Reads go through the github skill's wrapper, which owns token selection and fallback consent.
GH = str(Path(__file__).resolve().parents[2] / "github/scripts/gh-with-env-token")


class GhError(Exception):
    pass


def gh(args: list[str]) -> Any:
    result = subprocess.run([GH, *args], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise GhError(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else f"gh exited {result.returncode}")
    return json.loads(result.stdout)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def assess(fork: dict, owner: str, run: Runner, now: datetime, active_days: int) -> dict:
    name = fork["nameWithOwner"]
    parent = fork.get("parent") or {}
    upstream = f"{parent['owner']['login']}/{parent['name']}" if parent else None
    report = {"fork": name, "upstream": upstream, "pushed_at": fork.get("pushedAt"),
              "pull_requests": [], "branches": [], "reasons": [], "errors": []}
    if fork.get("isArchived"):
        report.update(disposition="archived", reasons=["already archived"])
        return report
    if not upstream:
        report.update(disposition="needs-review", errors=["upstream is unavailable"])
        return report
    since = now - timedelta(days=active_days)
    try:
        base = run(["api", f"repos/{upstream}"])["default_branch"]
        branches = run(["api", "--paginate", "--slurp", f"repos/{name}/branches?per_page=100"])
    except GhError as exc:
        report.update(disposition="needs-review", errors=[str(exc)])
        return report
    pulls = []
    for branch in (item for page in branches for item in page):
        entry = {"name": branch["name"], "sha": branch["commit"]["sha"]}
        try:
            # PRs are found by their source branch, whoever opened them; a PR whose branch was deleted is closed.
            found = run(["api", "--paginate", "--slurp", "-X", "GET", f"repos/{upstream}/pulls",
                         "-f", f"head={owner}:{branch['name']}", "-f", "state=all", "-f", "per_page=100"])
            # The SHA keeps branch names such as `a#b` out of the URL path.
            compare = run(["api", f"repos/{upstream}/compare/{base}...{owner}:{entry['sha']}",
                           "--jq", "{ahead_by, status}"])
            entry["ahead_by"] = compare["ahead_by"]
        except GhError as exc:
            entry["ahead_by"] = None
            if "No common ancestor" in str(exc):
                # An orphan branch such as gh-pages shares no history, so none of it is upstream.
                entry["unrelated_history"] = True
            else:
                report["errors"].append(f"{branch['name']}: {exc}")
                found = []
        found = [pull for page in found for pull in page]
        pulls.extend(found)
        delivered = [pull["number"] for pull in found if pull["merged_at"] and pull["head"]["sha"] == entry["sha"]]
        if entry["ahead_by"] and delivered:
            # A squash or rebase merge leaves the fork's commits "ahead" although the change is upstream.
            entry["delivered_by_pr"] = delivered[0]
        report["branches"].append(entry)
    report["pull_requests"] = [{"number": pull["number"], "state": "merged" if pull["merged_at"] else pull["state"],
                                "url": pull["html_url"]} for pull in pulls]

    open_pulls = [pull["number"] for pull in pulls if pull["state"] == "open"]
    recent_pulls = [pull for pull in pulls if parse_time(pull["created_at"]) >= since]
    if open_pulls:
        report["reasons"].append("open PR " + ", ".join(f"#{number}" for number in open_pulls))
    if fork.get("pushedAt") and parse_time(fork["pushedAt"]) >= since:
        report["reasons"].append(f"pushed within {active_days} days")
    if len(recent_pulls) > 1:
        report["reasons"].append(f"{len(recent_pulls)} PRs within {active_days} days")
    if report["reasons"]:
        report["disposition"] = "keep"
        return report
    if report["errors"]:
        report["disposition"] = "needs-review"
        return report
    unsent = [entry["name"] for entry in report["branches"]
              if (entry["ahead_by"] or entry.get("unrelated_history")) and "delivered_by_pr" not in entry]
    if unsent:
        report.update(disposition="archive", reasons=["commits never sent upstream on " + ", ".join(unsent)])
        return report
    try:
        # Search only delete candidates; code search is rate-limited and sees only indexed, visible repos.
        search = run(["api", "-X", "GET", "search/code", "-f", f"q={name} user:{owner}",
                      "--jq", "{incomplete_results, repos: ([.items[].repository.full_name] | unique)}"])
        if search["incomplete_results"]:
            raise GhError("search timed out with incomplete results")
    except GhError as exc:
        report.update(disposition="needs-review", errors=[f"dependent search: {exc}"])
        return report
    hits = [repo for repo in search["repos"] if repo.lower() != name.lower()]
    if hits:
        report.update(disposition="keep", reasons=["referenced by " + ", ".join(hits)])
    else:
        report.update(disposition="delete", reasons=["every PR is merged or closed and no branch holds unsent commits"])
    return report


def dispositions(owner: str, *, active_days: int = 90, run: Runner = gh, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    forks = run(["repo", "list", owner, "--fork", "--limit", "1000",
                 "--json", "nameWithOwner,parent,pushedAt,isArchived"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        reports = list(pool.map(lambda fork: assess(fork, owner, run, now, active_days), forks))
    reports.sort(key=lambda item: item["fork"].lower())
    return {
        "owner": owner,
        "active_days": active_days,
        "forks": reports,
        "complete": not any(item["errors"] for item in reports),
        # Code search finds references in visible, indexed repositories; it cannot prove nothing depends on a fork.
        "dependents_proven_absent": False,
    }
