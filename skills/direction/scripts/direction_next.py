# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
from collections.abc import Callable
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from skills.github.scripts.github_plan_status import events, next_actor, waits


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
    labels = {label if isinstance(label, str) else label["name"] for label in item.get("labels", [])}
    if next_actor.direction(record["actor"], record["wait"]) and "direction" not in labels:
        result.append({"kind": "direction"})
    resolved = waits.resolve_events(record["wait"], now=now, one=one, listing=listing)
    if resolved != record["wait"]:
        result.append({"kind": "resolve", "before": record["wait"], "after": resolved})
    actor = record["actor"]
    updated = item.get("updated_at")
    if actor and actor.startswith("agent") and record["wait"]["state"] == "clear" and updated:
        timestamp = dt.datetime.fromisoformat(updated.replace("Z", "+00:00"))
        key = hashlib.sha256(json.dumps([actor, record["action"]]).encode()).hexdigest()[:20]
        marker = f"<!-- github-plan:next-nudge {key} -->"
        nudged = any(
            marker in comment.get("body", "")
            and ((comment.get("user") or {}).get("login") or comment.get("author") or "").casefold() in trusted
            for comment in comments
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
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        if completed.returncode:
            raise ValueError(completed.stdout.strip() or completed.stderr.strip() or "Next action failed")
        return json.loads(completed.stdout)


def comment(repo: str, number: int, body: str) -> None:
    completed = subprocess.run(
        [str(ROOT / "skills/github/scripts/gh-comment"), "issue", str(number), "--repo", repo],
        input=body,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if completed.returncode:
        raise ValueError(completed.stdout.strip() or completed.stderr.strip() or "Next comment failed")


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
) -> bool:
    observed = run(repo, ["show", str(number), "--full"])["issue"]
    current = {**observed, "html_url": observed["url"], "user": {"login": observed["provenance"]["author"]}}
    comments = [{**row, "user": {"login": row["author"]}} for row in observed["comments"]]
    if action not in actions(current, comments, trusted=trusted, now=now, one=one, listing=listing):
        return False
    kind = action["kind"]
    if kind in {"reopen", "retire_reopen"}:
        if kind == "reopen":
            completed = subprocess.run(
                [str(ROOT / "skills/github/scripts/gh-issue"), "reopen", str(number), "--repo", repo],
                text=True,
                capture_output=True,
                timeout=60,
            )
            if completed.returncode:
                raise ValueError(completed.stdout.strip() or completed.stderr.strip() or "Reopen failed")
        cleared = {"version": 1, "state": "clear", "evidence": "Daily sweep handled " + events.describe(action["trigger"])}
        post(repo, number, "Handled reopen event: " + events.describe(action["trigger"]) + ".\n\n" + events.marker(cleared))
        from skills.github.scripts import github_issue

        github_issue.edit_issue(number, repo=repo, remove_labels=[events.INDEX_LABEL])
    elif kind == "resolve":
        record = next_actor.record(current, comments, trusted)
        status = ""
        if action["after"]["state"] == "clear" and (record["actor"] or "").startswith("event:"):
            status = "## Current Status\n\n" + next_actor.write(record["status"], "agent") + "\n\n"
        post(repo, number, status + "The recorded event completed.\n\n" + waits.marker(action["after"]))
        run(repo, ["reconcile-status", str(number), "--apply"])
    elif kind == "direction":
        from skills.github.scripts import github_issue

        github_issue.edit_issue(number, repo=repo, add_labels=["direction"])
    elif kind == "nudge":
        post(
            repo,
            number,
            f"Next action for {action['actor']} is still available after 24 hours: {action['action']}\n\n{action['marker']}",
        )
    else:
        raise ValueError("Unknown next action")
    return True


def scheduled_issue(
    repo: str, pair: list[dict[str, Any]], *, candidates: list[dict[str, Any]], run: Callable = command, post: Callable = comment
) -> dict[str, Any]:
    latest = pair[0]
    marker = f"<!-- github-plan:scheduled-job {latest['workflow_id']} -->"
    title = f"Scheduled job: {latest['name']} failed twice"
    exact = [item for item in candidates if marker in item.get("body", "") or item.get("title") == title]
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
    )
    if not exact:
        return run(repo, ["create", title], body)
    item = exact[0]
    if item.get("state") == "closed":
        completed = subprocess.run(
            [str(ROOT / "skills/github/scripts/gh-issue"), "reopen", str(item["number"]), "--repo", repo],
            text=True,
            capture_output=True,
            timeout=60,
        )
        if completed.returncode:
            raise ValueError(completed.stdout.strip() or completed.stderr.strip() or "Scheduled issue reopen failed")
        post(
            repo,
            item["number"],
            "## Current Status\n\nNext actor: agent\nNext action: Diagnose the repeated scheduled failures.\n\n" + links,
        )
    return {"issue": item}
