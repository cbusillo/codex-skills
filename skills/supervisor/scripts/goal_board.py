#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Rebuild the Supervisor's goal board from every open Track issue on OWNER/direction."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
import time
from typing import Any

CATALOG = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(CATALOG))
sys.path.insert(0, str(CATALOG / "skills/github/scripts"))
from skills.github.scripts import github_direction_next, github_next_queue, github_plan_status, github_plan_wait, github_work_graph
import github_read
from skills.github.scripts.github_identity import automation_login
from skills.github.scripts.ready_handoff import HEAD, READY, WAITING, handoff_line, timestamp

UNKNOWN = "unknown, not refreshed"
DECISION = re.compile(r"(?i)^\W*director decision\b")
CHILD_LIMIT = github_work_graph.CHILD_LIMIT
LABEL_LIMIT = github_work_graph.LABEL_LIMIT
PR_LIMIT = github_work_graph.PR_LIMIT
NEXT_ACTION_CHARS = 200
OPEN_LABELS = tuple(github_plan_status.DEFAULT_STATUS_LABELS[key] for key in ("active", "blocked", "waiting", "stale"))

def login(author: dict[str, Any] | None) -> str:
    return github_work_graph.graphql_author(author)["login"].casefold()


def truncated(connection):
    return connection["totalCount"] > len(connection["nodes"])


def status_label(labels, unread):
    names = [node["name"] for node in labels["nodes"]]
    found = next((name for name in names if name.startswith("plan:")), None)
    if found is None and truncated(labels):
        unread.append("status label")
        return UNKNOWN
    return found


def derived_status(node: dict[str, Any], trusted: set[str], unread: list[str]) -> dict[str, Any]:
    """Native blockers and the last trusted structured wait own current status."""
    unknown = {"status": UNKNOWN, "blocked_by": [], "waits": [], "structured_wait": None}
    blockers = node.get("blockedBy")
    if not isinstance(blockers, dict) or truncated(blockers):
        unread.append("native blockers")
        return unknown
    comments = [github_work_graph.graphql_comment(comment) for comment in node["comments"]["nodes"]]
    source = {"body": node.get("body") or "", "state": node["state"], "state_reason": node.get("stateReason"),
              "user": github_work_graph.graphql_author(node.get("author")),
              "labels": [item["name"] for item in node["labels"]["nodes"]]}
    try:
        # The shared resolver validates historical trusted records too. A clear
        # tail record cannot prove that unread history contains no malformed one.
        if truncated(node["comments"]):
            unread.append("structured wait")
            return unknown
        wait = github_plan_wait.current(source, comments, trusted)
        relationships = {"blocked_by": [github_work_graph.graphql_relationship(item) for item in blockers["nodes"]]}
        derived = github_plan_status.derive(source, relationships, wait)
    except (ValueError, KeyError, TypeError, AttributeError):
        unread.append("structured wait or native blockers")
        return unknown
    if truncated(node["labels"]):
        # Labels carry deliberate stale/done scope holds. An omitted label can
        # prevent activation even when blockers and waits were read completely.
        unread.append("status labels")
        return {**derived, "status": UNKNOWN}
    if derived["wait_review_required"] and node["state"] != "CLOSED":
        unread.append("structured wait")
        return {**derived, "status": UNKNOWN}
    if derived["scope_hold"]:
        return {**derived, "status": github_plan_status.DEFAULT_STATUS_LABELS.get(derived["scope_status"], "scope review")}
    return {**derived, "status": github_plan_status.DEFAULT_STATUS_LABELS[derived["status"]]}


def latest_decision(comments, trusted, unread):
    """The newest decision in the last comments is the latest; none there may hide an older one."""
    found = [node for node in comments["nodes"]
             if login(node.get("author")) in trusted and DECISION.match(node.get("body") or "")]
    if found:
        return max(found, key=lambda node: timestamp(node["createdAt"]))["url"]
    if truncated(comments):
        unread.append("older decisions")
    return None


def next_action(node, trusted, unread):
    """The live Next action from the issue's Current Status, on one line.

    A status comment as new as the body wins, so a long thread without one
    in the window settles only when the body is strictly newer than that window.
    """
    comments = node["comments"]
    body_at = node.get("lastEditedAt") or node["createdAt"]
    discussion: dict[str, Any] = {"body": node.get("body") or "", "comments": [
        {"author": login(comment.get("author")), "body": comment.get("body"), "created_at": comment["createdAt"]}
        for comment in comments["nodes"]]}
    in_window = any(comment["author"] in trusted
                    and "Current Status" in github_direction_next.section_map(comment["body"] or "")
                    for comment in discussion["comments"])
    in_body = "Current Status" in github_direction_next.section_map(discussion["body"])
    if truncated(comments) and not in_window and (
            not in_body or not comments["nodes"]
            or timestamp(body_at) <= min(timestamp(comment["createdAt"]) for comment in comments["nodes"])):
        unread.append("next action")
        return UNKNOWN
    if not in_window and not in_body:
        return "no Current Status"
    status = github_next_queue.current_status(discussion, trusted, body_recorded_at=body_at)
    action = " ".join(github_next_queue.status_field(status, "Next action").split())
    if not action:
        return "no Next action in Current Status"
    return action if len(action) <= NEXT_ACTION_CHARS else action[:NEXT_ACTION_CHARS - 1].rstrip() + "…"


def child_state(child):
    """A closed child is closed whatever its label says; an open label is noted as stale."""
    if child["state"] != "closed":
        return child["status"] or child["state"]
    label = child["recorded_status"]
    return f"closed (label {label} is stale)" if label in OPEN_LABELS else "closed"


def handoff(pr, trusted, unread):
    """Classify the latest trusted handoff with the merge train's shared rules."""
    latest = None
    for node in pr["comments"]["nodes"]:
        lines = (node.get("body") or "").splitlines()
        first = handoff_line(lines[0]) if lines else ""
        if login(node.get("author")) in trusted and (first == READY or first.startswith(WAITING)):
            key = (timestamp(node.get("updatedAt") or node["createdAt"]), node["databaseId"])
            if latest is None or key > latest[0]:
                latest = (key, first, node)
    if latest is None:
        if truncated(pr["comments"]):
            unread.append(f"handoff on {pr['url']}")
            return UNKNOWN
        return "no handoff"
    first, node = latest[1:]
    if first.startswith(WAITING):
        return "engineering ready, waiting"
    heads = [match.group("sha") for match in HEAD.finditer(node["body"])]
    return "ready" if heads == [pr["headRefOid"]] else "ready for an older head"


def child_row(node, trusted, wait_trusted=None):
    unread = []
    prs = node["closedByPullRequestsReferences"]
    if truncated(prs):
        unread.append(f"closing PRs beyond {PR_LIMIT}")
    return {"repository": node["repository"]["nameWithOwner"], "number": node["number"],
        "title": node["title"], "url": node["url"], "state": node["state"].lower(),
        **derived_status(node, wait_trusted if wait_trusted is not None else trusted, unread), "recorded_status": status_label(node["labels"], []),
        "sub_issues": node["subIssuesSummary"],
        "next_action": next_action(node, trusted, unread),
        "latest_decision": latest_decision(node["comments"], trusted, unread),
        "open_prs": [{"url": pr["url"], "head": pr["headRefOid"], "draft": pr["isDraft"],
            "mergeable": pr["mergeable"].lower(), "handoff": handoff(pr, trusted, unread)}
            for pr in prs["nodes"] if pr["state"] == "OPEN"],
        "unread": unread}


def track_row(node, trusted, wait_trusted=None):
    unread = []
    children = node["subIssues"]
    if truncated(children):
        unread.append(f"children beyond {CHILD_LIMIT}")
    return {"number": node["number"], "title": node["title"], "url": node["url"],
        "milestone": (node.get("milestone") or {}).get("title"),
        **derived_status(node, wait_trusted if wait_trusted is not None else trusted, unread),
        "next_action": next_action(node, trusted, unread),
        "latest_decision": latest_decision(node["comments"], trusted, unread),
        "children": [child_row(child, trusted, wait_trusted) for child in children["nodes"]],
        "unread": unread}


def goal_board(reader, owner, *, repo="direction", trusted, wait_trusted=None, max_pages=10):
    """Never carry an old status: an unread Track or part of one says unknown."""
    if wait_trusted is None:
        wait_trusted = github_plan_wait.authors(f"{owner}/{repo}")
    graph = github_work_graph.read_tracks(reader, owner, repo, max_pages=max_pages)
    tracks, errors = graph["tracks"], graph["errors"]
    rows = []
    for track in tracks:
        try:
            node = graph["nodes"][track["number"]]
            rows.append({**track_row(node, trusted, wait_trusted), "read": True})
        except (KeyError, TypeError, ValueError, AttributeError):
            rows.append({**track, "url": f"https://github.com/{owner}/{repo}/issues/{track['number']}",
                "read": False, "status": UNKNOWN})
    read = sum(1 for row in rows if row["read"])
    complete = (not errors and read == len(tracks)
        and not any(row["unread"] or any(child["unread"] for child in row["children"])
                    for row in rows if row["read"]))
    return {"tracks": rows, "coverage": {"discovered": len(tracks), "read": read, "complete": complete},
        "errors": errors}


def unread_text(row):
    # The status, next action and handoff fields already print their own unknown.
    return "".join(f"; {part} {UNKNOWN}" for part in row["unread"]
                   if part not in ("status label", "next action") and not part.startswith("handoff on "))


def action_text(row, links_only):
    # The links format is for public places, so it leaves out the action's prose.
    if links_only and row["next_action"] != UNKNOWN:
        return ""
    return f"; next action: {row['next_action']}"


def blocking_text(row, links_only):
    refs = ", ".join(item["html_url"] for item in row["blocked_by"])
    result = f"; blocked by: {refs or ('unknown' if row['status'] == UNKNOWN else 'none')}"
    if row["waits"] and not links_only:
        result += "; waiting for: " + "; ".join(wait["waiting_for"] for wait in row["waits"])
    return result


def markdown(board, *, links_only=False):
    discovered = board["coverage"]["discovered"]
    lines = [f"Goal board, checked at {board['checked_at']}. Tracks discovered: "
             f"{'unknown' if discovered is None else discovered}, read: {board['coverage']['read']}"
             + ("" if board["coverage"]["complete"] else " (incomplete)") + "."]
    for track in board["tracks"]:
        name = "" if links_only else f" {track['title']}"
        if not track["read"]:
            lines.append(f"\n- {track['url']}{name}: {UNKNOWN}")
            continue
        decision = f"; decision {track['latest_decision']}" if track["latest_decision"] else ""
        lines.append(f"\n- {track['url']}{name}: {track['status'] or 'no status label'}"
                     f"{blocking_text(track, links_only)}{action_text(track, links_only)}{decision}{unread_text(track)}")
        for child in track["children"]:
            title = "" if links_only else f" {child['title']}"
            decision = f"; decision {child['latest_decision']}" if child["latest_decision"] else ""
            prs = "".join(f"; PR {pr['url']} {'draft, ' if pr['draft'] else ''}{pr['handoff']}, mergeable {pr['mergeable']}"
                          for pr in child["open_prs"])
            lines.append(f"  - {child['url']}{title}: {child_state(child)}"
                         f"{blocking_text(child, links_only)}{action_text(child, links_only)}{decision}{prs}{unread_text(child)}")
    for error in board["errors"]:
        lines.append(f"\nError: {error}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", default="direction", help="direction repository name under --owner")
    parser.add_argument("--decision-author", action="append", default=[],
        help="trusted decision and handoff author besides the owner and automation login; repeatable")
    parser.add_argument("--format", choices=("json", "markdown", "links"), default="json",
        help="links omits titles, for posting where Client details must not appear")
    parser.add_argument("--deadline-seconds", type=float, default=120)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", args.repo):
        parser.error("owner and repo must be GitHub names")
    if not math.isfinite(args.deadline_seconds) or args.deadline_seconds <= 0:
        parser.error("deadline must be positive")
    trusted = {name.casefold() for name in
               [args.owner, automation_login() or "shiny-code-app[bot]", *args.decision_author]}
    reader = github_read.GitHubReader(operation="github.supervisor.goal_board", strict_actor=True,
        gh_prefix_args=github_read.automation_only_gh_prefix_args(), deadline_at=time.time() + args.deadline_seconds)
    checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        board = goal_board(reader, args.owner, repo=args.repo, trusted=trusted)
    except (github_read.GitHubReadError, ValueError, KeyError, TypeError) as error:
        board = {"tracks": [], "coverage": {"discovered": None, "read": 0, "complete": False},
            "errors": [f"Track discovery failed, every Track is {UNKNOWN}: {error}"]}
    board = {"ok": board["coverage"]["complete"], "owner": args.owner, "repository": f"{args.owner}/{args.repo}",
        "checked_at": checked_at, **board}
    if args.format == "json":
        print(json.dumps({**board, "diagnostics": reader.diagnostics()}))
    else:
        print(markdown(board, links_only=args.format == "links"))
    return 0 if board["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
