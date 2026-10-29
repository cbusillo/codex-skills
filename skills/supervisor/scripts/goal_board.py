#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Rebuild the Supervisor's goal board from every open Track issue on OWNER/direction."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
import time
from typing import Any
from check_changes import changes, fingerprint

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
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        unread.append(f"structured wait or native blockers ({type(error).__name__}: {error})")
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


def current_status(node, trusted, unread):
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
        return None
    return github_next_queue.current_status(discussion, trusted, body_recorded_at=body_at)


def next_action(node, trusted, unread):
    status = current_status(node, trusted, unread)
    if status == UNKNOWN:
        return UNKNOWN
    if status is None:
        return "no Current Status"
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


def read_part(part: str, unread: list[str], read: Callable[[], Any], fallback: Any = UNKNOWN) -> Any:
    try:
        return read()
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        unread.append(f"{part} ({type(error).__name__}: {error})")
        return fallback


def open_prs(node: dict[str, Any], trusted: set[str], unread: list[str]) -> list[dict[str, Any]]:
    prs = node["closedByPullRequestsReferences"]
    if truncated(prs):
        unread.append(f"closing PRs beyond {PR_LIMIT}")
    rows = []
    for index, pr in enumerate(prs["nodes"]):
        row = read_part(f"closing PR at index {index}", unread,
            lambda pr=pr: pr_row(pr, trusted, unread) if pr["state"] == "OPEN" else None, None)
        if row is not None:
            rows.append(row)
    return rows


def pr_row(pr: dict[str, Any], trusted: set[str], unread: list[str]) -> dict[str, Any]:
    return {"url": pr["url"],
        "head": read_part(f"head on {pr['url']}", unread, lambda: pr["headRefOid"]),
        "draft": read_part(f"draft status on {pr['url']}", unread, lambda: pr["isDraft"], None),
        "mergeable": read_part(f"mergeability on {pr['url']}", unread, lambda: pr["mergeable"].lower()),
        "handoff": read_part(f"handoff on {pr['url']}", unread, lambda: handoff(pr, trusted, unread))}


def issue_row(node: dict[str, Any], trusted: set[str], wait_trusted: set[str] | None, unread: list[str]) -> dict[str, Any]:
    return {"number": node["number"], "title": node["title"], "url": node["url"],
        **read_part("status", unread,
            lambda: derived_status(node, wait_trusted if wait_trusted is not None else trusted, unread),
            {"status": UNKNOWN, "blocked_by": [], "waits": [], "structured_wait": None}),
        "next_action": read_part("next action", unread, lambda: next_action(node, trusted, unread)),
        "latest_decision": read_part("latest decision", unread,
            lambda: latest_decision(node["comments"], trusted, unread), None)}


def child_row(node: dict[str, Any], trusted: set[str], wait_trusted: set[str] | None = None) -> dict[str, Any]:
    unread: list[str] = []
    return {**issue_row(node, trusted, wait_trusted, unread),
        "repository": read_part("repository", unread, lambda: node["repository"]["nameWithOwner"]),
        "state": read_part("state", unread, lambda: node["state"].lower()),
        "recorded_status": read_part("status label", unread, lambda: status_label(node["labels"], unread)),
        "sub_issues": read_part("sub-issue summary", unread, lambda: node["subIssuesSummary"], None),
        "open_prs": read_part("closing PRs", unread, lambda: open_prs(node, trusted, unread), []),
        "unread": unread}


def child_rows(node: dict[str, Any], trusted: set[str], wait_trusted: set[str] | None, unread: list[str]) -> list[dict[str, Any]]:
    children = node["subIssues"]
    if truncated(children):
        unread.append(f"children beyond {CHILD_LIMIT}")
    rows = []
    for index, child in enumerate(children["nodes"]):
        row = read_part(f"child at index {index}", unread, lambda child=child: child_row(child, trusted, wait_trusted), None)
        if row is not None:
            rows.append(row)
    return rows


def track_row(node: dict[str, Any], trusted: set[str], wait_trusted: set[str] | None = None) -> dict[str, Any]:
    unread: list[str] = []
    status = read_part("milestone proof status", unread, lambda: current_status(node, trusted, unread))
    return {**issue_row(node, trusted, wait_trusted, unread),
        "closure_review": {**read_part("milestone proof", unread,
            lambda: github_direction_next.milestone_closure_review(node.get("body") or "",
                "" if status == UNKNOWN else status), {"track_proof": None, "recorded_status": None}),
            "discussion_complete": status != UNKNOWN},
        "milestone": read_part("milestone", unread, lambda: (node.get("milestone") or {}).get("title"), None),
        "children": read_part("children", unread, lambda: child_rows(node, trusted, wait_trusted, unread), []),
        "unread": list(dict.fromkeys(unread))}


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
        except (KeyError, TypeError) as error:
            if track["number"] in graph["nodes"]:
                errors.append(f"Track #{track['number']} unread: {type(error).__name__}: {error}")
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
                   if part not in ("status label", "next action") and not part.startswith("next action (")
                   and (not part.startswith("handoff on ") or " (" in part))


def action_text(row, links_only):
    # The links format is for public places, so it leaves out the action's prose.
    if links_only and row["next_action"] != UNKNOWN:
        return ""
    label = next((part for part in row["unread"] if part.startswith("next action (")), "next action")
    return f"; {label}: {row['next_action']}"


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
        review = track["closure_review"]
        lines.append(f"\n- {track['url']}{name}: {track['status'] or 'no status label'}"
                     f"{blocking_text(track, links_only)}{action_text(track, links_only)}{decision}{unread_text(track)}")
        if not links_only:
            proof = " ".join((review["track_proof"] or UNKNOWN).split())
            state = " ".join(github_next_queue.status_field(review["recorded_status"] or "", "State").split()) or UNKNOWN
            proof = proof[:NEXT_ACTION_CHARS] + "…" if len(proof) > NEXT_ACTION_CHARS else proof
            state = state[:NEXT_ACTION_CHARS] + "…" if len(state) > NEXT_ACTION_CHARS else state
            lines.append(f"  - Proof to assess: {proof}; recorded state: {state}")
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
    parser.add_argument("--changes-state", type=Path, help="print changed record pointers and current coverage; save only fingerprints")
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
    if args.changes_state:
        print(json.dumps(board_changes(board, args.changes_state, sorted(trusted))))
    elif args.format == "json":
        print(json.dumps({**board, "diagnostics": reader.diagnostics()}))
    else:
        print(markdown(board, links_only=args.format == "links"))
    return 0 if board["ok"] else 1


def board_changes(board: dict, state: Path, trusted: list[str]) -> dict:
    rows = []
    for track in board["tracks"]:
        for record in (track, *track.get("children", [])):
            source = {key: value for key, value in record.items() if key != "children"}
            rows.append({"url": record["url"], "track_url": track["url"],
                         **{key: record[key] for key in ("status", "state", "latest_decision", "open_prs", "unread", "read") if key in record},
                         "revision": fingerprint(source)})
    return {"records": changes(rows, state, [board["repository"], *trusted], key="url", complete=board["coverage"]["complete"]),
            "coverage": board["coverage"], "checked_at": board["checked_at"], "errors": board["errors"]}


if __name__ == "__main__":
    raise SystemExit(main())
