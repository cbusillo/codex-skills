# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "skills/github/scripts"))
from skills.github.scripts import github_api, github_comment, github_issue
from skills.github.scripts.github_plan_status import events, next_actor, waits


@contextmanager
def deadline(deadline_at: float | None) -> Iterator[None]:
    previous = os.environ.get("GITHUB_RETRY_DEADLINE_AT")
    if deadline_at is not None:
        os.environ["GITHUB_RETRY_DEADLINE_AT"] = str(min(deadline_at, float(previous)) if previous else deadline_at)
    try:
        if github_api.remaining_retry_timeout_seconds() <= 0:
            raise ValueError("next_action_deadline_exceeded")
        yield
    finally:
        if previous is None:
            os.environ.pop("GITHUB_RETRY_DEADLINE_AT", None)
        else:
            os.environ["GITHUB_RETRY_DEADLINE_AT"] = previous


def actions(
    item: dict[str, Any], comments: list[dict[str, Any]], *, trusted: set[str], now: dt.datetime, one: Callable, listing: Callable
) -> list[dict[str, Any]]:
    record = next_actor.record(item, comments, trusted)
    trigger = record["reopen"]
    if item.get("state") == "closed":
        if trigger and events.fired(trigger, now=now, one=one, listing=listing):
            return [{"kind": "reopen", "trigger": trigger}]
        return []
    result = [{"kind": "retire_reopen", "trigger": trigger}] if trigger and trigger.get("state") != "clear" else []
    labels = {(label if isinstance(label, str) else label["name"]).casefold() for label in item.get("labels", [])}
    if next_actor.direction(record["actor"], record["wait"]) and "direction" not in labels:
        result.append({"kind": "direction"})
    resolved = waits.resolve_events(record["wait"], now=now, one=one, listing=listing)
    if resolved != record["wait"]:
        result.append({"kind": "resolve", "before": record["wait"], "after": resolved})
    actor = record["actor"]
    updated = item.get("updated_at")
    blocked = (item.get("issue_dependencies_summary") or {}).get("blocked_by", 0)
    if (
        actor
        and actor.startswith("agent")
        and record["wait"]["state"] == "clear"
        and isinstance(updated, str)
        and "plan:blocked" not in labels
        and not blocked
    ):
        timestamp = dt.datetime.fromisoformat(updated.replace("Z", "+00:00"))
        key = hashlib.sha256(json.dumps([actor, record["action"]]).encode()).hexdigest()[:20]
        marker = f"<!-- github-plan:next-nudge {key} -->"
        nudged = any(
            marker in entry.get("body", "")
            and ((entry.get("user") or {}).get("login") or entry.get("author") or "").casefold() in trusted
            for entry in comments
        )
        if timestamp.tzinfo is None:
            raise ValueError("Next record activity time lacks a timezone")
        if now - timestamp >= dt.timedelta(hours=24) and not nudged:
            result.append({"kind": "nudge", "actor": actor, "action": record["action"], "marker": marker})
    return result


def scheduled_failures(runs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    workflows: dict[int, list[dict[str, Any]]] = {}
    for run in runs:
        if run.get("event") == "schedule":
            workflows.setdefault(run["workflow_id"], []).append(run)
    failed = []
    for entries in workflows.values():
        entries.sort(key=lambda row: (row["run_number"], row.get("run_attempt", 1)), reverse=True)
        pair = entries[:2]
        if len(pair) == 2 and all(
            run.get("status") == "completed" and run.get("conclusion") in {"failure", "timed_out", "startup_failure"} for run in pair
        ):
            failed.append(pair)
    return failed


def command(repo: str, args: list[str], body: str | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="next-sweep-") as scratch:
        argv = ["uv", "run", str(ROOT / "skills/github/scripts/gh-plan.py"), "--repo", repo, *args]
        if body is not None:
            path = pathlib.Path(scratch) / "body.md"
            path.write_text(body)
            argv.extend(["--body-file", str(path)])
        timeout = min(60, github_api.remaining_retry_timeout_seconds())
        if timeout <= 0:
            raise ValueError("next_action_deadline_exceeded")
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if completed.returncode:
            raise ValueError(completed.stdout.strip() or completed.stderr.strip() or "Next action failed")
        return json.loads(completed.stdout)


def comment(repo: str, number: int, body: str) -> None:
    github_comment.comment("issue", number, body, repo=repo)


def apply(
    repo: str,
    number: int,
    action: dict[str, Any],
    *,
    trusted: set[str],
    now: dt.datetime,
    one: Callable,
    listing: Callable,
    run: Callable = command,
    post: Callable = comment,
    prepare_source: Callable | None = None,
) -> bool:
    current = one(f"repos/{repo}/issues/{number}")
    comments = listing(f"repos/{repo}/issues/{number}/comments")
    if prepare_source is not None:
        current = prepare_source(repo, current, comments)
    if current.get("state") not in {"open", "closed"} or not isinstance((current.get("user") or {}).get("login"), str):
        raise ValueError("Next action source state or author unavailable")
    if action not in actions(current, comments, trusted=trusted, now=now, one=one, listing=listing):
        return False
    kind = action["kind"]
    if kind in {"reopen", "retire_reopen"}:
        if kind == "reopen":
            github_issue.set_issue_state(number, state="open", state_reason="reopened", repo=repo)
        cleared = events.clear("Daily sweep handled " + events.describe(action["trigger"]))
        post(repo, number, "Handled reopen event: " + events.describe(action["trigger"]) + ".\n\n" + events.marker(cleared))
        github_issue.edit_issue(number, repo=repo, remove_labels=[events.INDEX_LABEL])
        run(repo, ["reconcile-status", str(number), "--apply"])
    elif kind == "resolve":
        record = next_actor.record(current, comments, trusted)
        if action["after"]["state"] == "clear" and (record["actor"] or "").startswith("event:"):
            run(repo, ["update-section", str(number), "Current Status"], next_actor.write(record["status"], "agent"))
        post(repo, number, "The recorded event completed.\n\n" + waits.marker(action["after"]))
        run(repo, ["reconcile-status", str(number), "--apply"])
    elif kind == "direction":
        github_issue.edit_issue(number, repo=repo, add_labels=["direction"])
    elif kind == "nudge":
        post(
            repo,
            number,
            f"Next action for {action['actor']} has had no activity for 24 hours: {action['action']}\n\n{action['marker']}",
        )
    else:
        raise ValueError("Unknown next action")
    return True


def scheduled_issue(
    repo: str, pair: list[dict[str, Any]], *, candidates: list[dict[str, Any]], run: Callable = command, post: Callable = comment
) -> dict[str, Any]:
    latest = pair[0]
    marker = f"Scheduled job ID: {latest['workflow_id']}"
    title = f"Scheduled job {latest['workflow_id']}: {latest['name']} failed twice"
    exact = [item for item in candidates if marker in item.get("body", "").splitlines() or item.get("title") == title]
    if len(exact) > 1:
        raise ValueError("Multiple issues track the same scheduled job")
    links = "\n".join(f"- {entry['html_url']} ({entry['conclusion']})" for entry in reversed(pair))
    body = (
        "<!-- github-plan:managed-provenance -->\n\n## Why\n\nTwo consecutive scheduled runs failed.\n\n"
        + links
        + "\n\n## Finish Line\n\nFix the cause and observe a passing scheduled run.\n\n## Current Status\n\n"
        "Next actor: agent\nNext action: Read both run logs, compare the last passing run, and fix the cause.\n\n"
        + waits.marker(waits.reviewed(evidence="Scheduled run failures require agent investigation."))
        + "\n\n"
        + marker
        + "\n\n"
        + f"Reported scheduled run: {latest['id']}"
    )
    if not exact:
        return run(repo, ["create", title], body)
    item = exact[0]
    reported = f"Reported scheduled run: {latest['id']}"
    if reported in item.get("body", "") or any(reported in entry.get("body", "") for entry in item.get("_comments", [])):
        return {"issue": item, "already_reported": True}
    if item.get("state") == "closed":
        github_issue.set_issue_state(item["number"], state="open", state_reason="reopened", repo=repo)
    post(
        repo,
        item["number"],
        "## Current Status\n\nNext actor: agent\nNext action: Diagnose the repeated scheduled failures.\n\n"
        + links
        + "\n\n"
        + reported,
    )
    return {"issue": item}
