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
    for raw in re.findall(r"<!-- github-plan:claim (.*?) -->", text):
        record = json.loads(raw)
        if not isinstance(record, dict) or not all(
            isinstance(record.get(key), str) and record[key]
            for key in ("worker", "session", "branch", "claimed_at")
        ):
            raise ValueError("Malformed claim record; preserve ownership for owner review")
        found.append(record)
    if text.count(MARKER) != len(found):
        raise ValueError("Malformed claim marker; preserve ownership for owner review")
    return found


def same_owner(record: dict[str, str], claim: dict[str, str]) -> bool:
    return all(record.get(key) == claim[key] for key in ("worker", "session", "branch"))


def references_issue(text: str, number: int) -> bool:
    return bool(re.search(rf"(?<!\d){number}(?!\d)", text))


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
        r"(?im)owned by|claimed by|\bworker\s*:|\bsession\s*:|\bbranch\s*:|\bstate:\s*active",
        status,
    ):
        if any(claim[key] not in status for key in ("worker", "session", "branch")):
            conflicts.append({"source": "current_status", "text": status, "certainty": "ambiguous"})
    released: dict[tuple[str, str], int] = {}
    for index, comment in enumerate(comments):
        match = re.match(r"Released by (\S+)", comment.get("body") or "")
        if match:
            author = (comment.get("user") or {}).get("login", "")
            released[match.group(1), author] = index
    for index, comment in enumerate(comments):
        text = comment.get("body") or ""
        parsed = records(text)
        author = (comment.get("user") or {}).get("login", "")
        legacy = re.match(r"Claimed by (\S+)", text)
        if legacy and not parsed:
            worker = legacy.group(1)
            if released.get((worker, author), -1) > index:
                continue
            if worker != claim["worker"] or any(claim[key] not in text for key in ("session", "branch")):
                conflicts.append({"source": "comment", "id": comment.get("id"), "text": text,
                                  "certainty": "current_or_stale"})
            else:
                owned.append({**claim, "claimed_at": comment.get("created_at") or claim["claimed_at"]})
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
    if not re.search(rf"[:/]{re.escape(repo)}(?:\.git)?$", remote):
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
            if not isinstance(sessions, list) or any(not isinstance(s, dict) for s in sessions):
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


def artifact_evidence(
    inventory: dict[str, Any], pulls: list[dict[str, Any]], number: int,
    claim: dict[str, str], *, own_record: bool,
) -> list[dict[str, Any]]:
    conflicts = []
    for source in ("local_branches", "remote_branches"):
        for branch in inventory[source]:
            if references_issue(branch, number) and not (own_record and branch == claim["branch"]):
                conflicts.append({"source": source, "branch": branch, "certainty": "current_or_stale"})
    for tree in inventory["worktrees"]:
        if references_issue(tree["branch"] + "/" + pathlib.Path(tree["path"]).name, number):
            if not (own_record and tree["branch"] == claim["branch"]):
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
        text = "\n".join([pull.get("title") or "", pull.get("body") or "", branch])
        if re.search(rf"(?:#|/issues/){number}(?!\d)", text) or references_issue(branch, number):
            if not (own_record and branch == claim["branch"]):
                conflicts.append({"source": "open_pr", "number": pull["number"], "branch": branch})
    return conflicts
