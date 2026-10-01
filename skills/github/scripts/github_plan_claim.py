# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Ownership evidence for the planning claim command; no GitHub writes."""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
from typing import Any


MARKER = "github-plan:claim "


def marker(claim: dict[str, str]) -> str:
    return "<!-- " + MARKER + json.dumps(claim, sort_keys=True) + " -->"


def records(text: str) -> list[dict[str, str]]:
    found = []
    # Format examples and incidental prose are not live ownership records.
    text = re.sub(r"(?ms)^```[^\n]*\n.*?^```[^\n]*$", "", text)
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("<!-- " + MARKER)]
    for line in lines:
        match = re.fullmatch(r"<!-- github-plan:claim (.*?) -->", line)
        if not match:
            raise ValueError("Malformed claim marker; preserve ownership for owner review")
        raw = match.group(1)
        record = json.loads(raw)
        if not isinstance(record, dict) or not all(
            isinstance(record.get(key), str) and record[key]
            for key in ("worker", "session", "branch", "claimed_at")
        ):
            raise ValueError("Malformed claim record; preserve ownership for owner review")
        found.append(record)
    return found


def same_owner(record: dict[str, str], claim: dict[str, str]) -> bool:
    return all(record.get(key) == claim[key] for key in ("worker", "session", "branch"))


def references_issue(text: str, number: int) -> bool:
    return bool(re.search(rf"(?i)(?:\bissue[-_ /#]?|\bgo[-_ ]?|\bagy[-_]|#|/issues/){number}(?!\d)", text)
                or re.search(rf"(?:^|/){number}[-_]", text))


def discussion_evidence(
    status: str, comments: list[dict[str, Any]], claim: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Old claims remain ambiguous until explicitly released; age is never a lease."""
    conflicts: list[dict[str, Any]] = []
    owned = []
    status_records = records(status)
    for record in status_records:
        if same_owner(record, claim):
            owned.append(record)
        else:
            conflicts.append({"source": "current_status", "record": record})
    if not status_records and re.search(
        r"(?im)owned by|claimed by|\bworker\s*:|\bsession\s*:",
        status,
    ):
        if any(claim[key] not in status for key in ("worker", "session", "branch")):
            conflicts.append({"source": "current_status", "text": status, "certainty": "ambiguous"})
    released: dict[tuple[str, str], int] = {}
    released_ids: dict[tuple[int, str], int] = {}
    for index, comment in enumerate(comments):
        match = re.match(r"Released by (\S+)", comment.get("body") or "")
        if match:
            author = (comment.get("user") or {}).get("login", "")
            worker = match.group(1)
            prior_sessions = {
                record["session"] for prior in comments[:index]
                if (prior.get("user") or {}).get("login", "") == author
                for record in records(prior.get("body") or "") if record["worker"] == worker
            }
            # A reused token cannot release a different native session.
            if len(prior_sessions) <= 1:
                released[worker, author] = index
        match_id = re.match(r"Released claim (\d+)(?:\s|$)", comment.get("body") or "")
        if match_id:
            author = (comment.get("user") or {}).get("login", "")
            released_ids[int(match_id.group(1)), author] = index
    for index, comment in enumerate(comments):
        text = comment.get("body") or ""
        parsed = records(text)
        author = (comment.get("user") or {}).get("login", "")
        if released_ids.get((comment.get("id"), author), -1) > index:
            continue
        legacy = re.match(r"Claimed by (\S+)", text)
        if legacy and not parsed:
            worker = legacy.group(1)
            if released.get((worker, author), -1) > index:
                continue
            if worker != claim["worker"] or any(claim[key] not in text for key in ("session", "branch")):
                conflicts.append({"source": "comment", "id": comment.get("id"), "text": text,
                                  "certainty": "current_or_stale"})
            else:
                owned.append({**claim, "claimed_at": comment.get("created_at") or claim["claimed_at"], "_legacy": "yes"})
        for record in parsed:
            if released.get((record["worker"], author), -1) > index:
                continue
            if same_owner(record, claim):
                owned.append(record)
            else:
                conflicts.append({"source": "comment", "id": comment.get("id"), "record": record,
                                  "certainty": "current_or_stale"})
    return conflicts, owned


def run_read(argv: list[str], *, cwd: pathlib.Path | None = None) -> str:
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError(f"Ownership inventory failed: {argv[0]} {argv[1]} (exit {result.returncode})")
    return result.stdout


def local_inventory(repo: str, number: int) -> dict[str, Any]:
    remote = run_read(["git", "remote", "get-url", "origin"]).strip()
    if not re.search(rf"[:/]{re.escape(repo)}(?:\.git)?$", remote, re.IGNORECASE):
        raise ValueError("Run claim from a checkout of the target repository")
    worktrees = []
    for block in run_read(["git", "worktree", "list", "--porcelain"]).strip().split("\n\n"):
        fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
        branch = fields.get("branch", "").removeprefix("refs/heads/")
        path = fields.get("worktree", "")
        worktrees.append({"branch": branch, "path": path})
    local = run_read(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"]).splitlines()
    remote_branches = [
        line.split("\t", 1)[1].removeprefix("refs/heads/")
        for line in run_read(["git", "ls-remote", "--heads", "origin"]).splitlines()
    ]
    sessions: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {"codex": {"status": "unavailable", "reason": "no CLI peer inventory"}}
    if shutil.which("claude"):
        try:
            sessions = json.loads(run_read(["claude", "agents", "--json"], cwd=pathlib.Path.home()))
            if not isinstance(sessions, list) or any(
                not isinstance(s, dict) or not isinstance(s.get("sessionId"), str)
                or not isinstance(s.get("cwd"), str) for s in sessions
            ):
                raise ValueError("Invalid Claude session inventory")
            coverage["claude"] = {"status": "available", "source": "claude agents --json",
                                  "scope": "native local active-session registry"}
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            coverage["claude"] = {"status": "unavailable", "reason": str(exc)}
            sessions = []
    else:
        coverage["claude"] = {"status": "unavailable", "reason": "Claude CLI not installed"}
    return {"worktrees": worktrees, "local_branches": local, "remote_branches": remote_branches,
            "sessions": sessions, "session_coverage": coverage, "issue": number}


def retained_branch(comments: list[dict[str, Any]], comment_id: int) -> str:
    original = next((c for c in comments if c.get("id") == comment_id), None)
    if original is None:
        raise ValueError("Resume source claim comment is missing")
    parsed = records(original.get("body") or "")
    if len(parsed) != 1:
        raise ValueError("Resume source must contain one structured claim record")
    author = (original.get("user") or {}).get("login")
    if not author:
        raise ValueError("Resume source author is unavailable")
    source_index = comments.index(original)
    for comment in comments[source_index + 1:]:
        if (comment.get("user") or {}).get("login") != author:
            continue
        first = (comment.get("body") or "").splitlines()[0:1]
        if first in ([f"Released claim {comment_id}"], [f"Released by {parsed[0]['worker']}"]):
            return parsed[0]["branch"]
    raise ValueError("Resume source claim has not been released by its author")


def artifact_evidence(
    inventory: dict[str, Any], pulls: list[dict[str, Any]], number: int,
    claim: dict[str, str], *, own_record: bool, retained: str | None = None, repo: str = "",
) -> list[dict[str, Any]]:
    conflicts = []
    def permitted(branch: str) -> bool:
        return (own_record and branch == claim["branch"]) or branch == retained

    for source in ("local_branches", "remote_branches"):
        for branch in inventory[source]:
            if references_issue(branch, number) and not permitted(branch):
                conflicts.append({"source": source, "branch": branch, "certainty": "current_or_stale"})
    for tree in inventory["worktrees"]:
        if references_issue(tree["branch"] + "/" + pathlib.Path(tree["path"]).name, number):
            if not permitted(tree["branch"]):
                conflicts.append({"source": "worktree", **tree, "certainty": "current_or_stale"})
    paths = {str(pathlib.Path(t["path"]).resolve()) for t in inventory["worktrees"]}
    for session in inventory["sessions"]:
        if session.get("sessionId") == claim["session"]:
            continue
        cwd = str(pathlib.Path(session.get("cwd") or "/").resolve())
        if cwd in paths and references_issue((session.get("name") or "") + "/" + pathlib.Path(cwd).name, number):
            conflicts.append({"source": "claude_session", "session": session.get("sessionId"),
                              "state": session.get("state") or session.get("status")})
    for pull in pulls:
        branch = (pull.get("head") or {}).get("ref", "")
        title = pull.get("title") or ""
        body = pull.get("body") or ""
        explicit_url = bool(repo and re.search(rf"https://github\.com/{re.escape(repo)}/issues/{number}(?!\d)", title + "\n" + body))
        linked = bool(re.search(rf"(?i)\b(?:refs?|fix(?:es|ed)?|clos(?:e|es|ed)|resolv(?:e|es|ed))\s+(?:#{number}|{re.escape(repo)}#{number})(?!\d)", body))
        titled = bool(re.search(rf"(?<![\w/])#{number}(?!\d)", title))
        if explicit_url or linked or titled or references_issue(branch, number):
            if not permitted(branch):
                conflicts.append({"source": "open_pr", "number": pull["number"], "branch": branch})
    return conflicts
