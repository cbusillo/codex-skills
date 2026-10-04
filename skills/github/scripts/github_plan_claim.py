# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Ownership evidence for the planning claim command; no GitHub writes."""

from __future__ import annotations

from datetime import datetime
import json
import pathlib
import re
import shutil
import subprocess
from typing import Any


MARKER = "github-plan:claim "


def no_wait_reason(reason: str, *, field: str) -> bool:
    """Recognize explicit absence of a wait without discarding wait clauses."""
    reason = reason.strip().casefold().rstrip(" .")
    if reason in {"none", "n/a", "nothing", "-", "no native issue blocker"}:
        return True
    # Only Blocked by may contain a separate explanatory sentence. A semicolon
    # (including a continued wait on the next line) remains a recorded blocker.
    if re.search(r"\b(?:wait(?:ing)?|await(?:ing)?|pending|parked|blocked|until|unless|except|but|after|requires?|needs?)\b", reason):
        return False
    if field == "Blocked by":
        return bool(re.fullmatch(r"(?:none|n/a|nothing|no native issue blocker)\.\s+[^;]+", reason))
    if field == "Waiting for":
        return bool(re.fullmatch(r"nothing for [\w -]+", reason))
    return False


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
    return (all(record.get(key) == claim[key] for key in ("worker", "session", "branch"))
            and record.get("refresh_pr") == claim.get("refresh_pr"))


def released_claim_id(text: str) -> int | None:
    """Read an exact first-line release, including a sentence-ending period."""
    match = re.match(r"Released claim (\d+)(?:\.(?=\s|$)|\s|$)", text)
    return int(match.group(1)) if match else None


def resumed_status(status: str, comments: list[dict[str, Any]], source_id: int | None) -> str:
    """Discard only an exact status marker superseded by its author's release."""
    if source_id is None:
        return status
    source = next((c for c in comments if c.get("id") == source_id), None)
    if source is None:
        return status
    source_records = records(source.get("body") or "")
    if len(source_records) != 1 or source_records[0] not in records(status):
        return status
    record = source_records[0]
    author = (source.get("user") or {}).get("login")
    if not author:
        return status
    for release in comments[comments.index(source) + 1:]:
        if ((release.get("user") or {}).get("login") != author
                or released_claim_id(release.get("body") or "") != source_id):
            continue
        try:
            released_at = datetime.fromisoformat(release["created_at"])
            marker_at = datetime.fromisoformat(record["claimed_at"])
            source_at = datetime.fromisoformat(source.get("updated_at") or source["created_at"])
            if released_at <= max(marker_at, source_at):
                continue
        except (KeyError, ValueError, TypeError):
            continue
        # Remove the marker and only its own identity assertions. Remaining
        # markers and unstructured ownership still go through the normal scan.
        status = "\n".join(line for line in status.splitlines()
                           if records(line) != [record])
        status = re.sub(rf"(?im)^\s*State:\s*Active;\s*owned by {re.escape(record['worker'])}\.?\s*$", "State: Active", status)
        for field, key in (("Worker", "worker"), ("Session", "session")):
            status = re.sub(rf"(?im)^\s*{field}:\s*{re.escape(record[key])}\.?\s*$", "", status)
        return status
    return status


def references_issue(text: str, number: int) -> bool:
    return bool(re.search(rf"(?i)(?:\bissue[-_ /#]?|\bgo[-_ ]?|\bagy[-_]|#|/issues/){number}(?!\d)", text)
                or re.search(rf"(?:^|/){number}[-_]", text))


def discussion_evidence(
    status: str, comments: list[dict[str, Any]], claim: dict[str, str], *, resume_from: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Old claims remain ambiguous until explicitly released; age is never a lease."""
    conflicts: list[dict[str, Any]] = []
    owned = []
    original_status = status
    status = resumed_status(status, comments, resume_from)
    status_records = records(status)
    for record in status_records:
        if same_owner(record, claim):
            owned.append(record)
        else:
            conflicts.append({"source": "current_status", "record": record})
    # Exclude only resource prose with separately scoped evidence and approval
    # responsibilities. Keep the broad fail-closed scan for other ownership,
    # including another assertion on the same line or elsewhere in the status.
    ownership_status = re.sub(
        r"(?im)^\s*(?:After these proposals,\s+)?"
        r"(?:Remaining |\d+ [\w-]+ and \d+ [\w-]+ )?(?:provider-only )?"
        r"(?:entries|records|resources)\b(?:\s+(?:would remain,|are))?\s+"
        r"owned by (?:(?!\b(?:owned|claimed) by\b)[\w -])+ for evidence and "
        r"(?:(?!\b(?:owned|claimed) by\b)[\w -])+ for (?:production )?disposition approval"
        r"(?:\.(?=\s|$)|(?=\n|$))",
        "",
        status,
    )
    if (not status_records or status != original_status) and re.search(
        r"(?im)owned by|claimed by|\bworker\s*:|\bsession\s*:",
        ownership_status,
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
        release_id = released_claim_id(comment.get("body") or "")
        if release_id is not None:
            author = (comment.get("user") or {}).get("login", "")
            released_ids[release_id, author] = index
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


def local_inventory(repo: str, number: int, *, cwd: pathlib.Path | None = None) -> dict[str, Any]:
    remote = run_read(["git", "remote", "get-url", "origin"], cwd=cwd).strip()
    if not re.search(rf"[:/]{re.escape(repo)}(?:\.git)?$", remote, re.IGNORECASE):
        raise ValueError("Run claim from a checkout of the target repository")
    worktrees = []
    for block in run_read(["git", "worktree", "list", "--porcelain"], cwd=cwd).strip().split("\n\n"):
        fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
        branch = fields.get("branch", "").removeprefix("refs/heads/")
        path = fields.get("worktree", "")
        worktrees.append({"branch": branch, "path": path})
    local = run_read(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd=cwd).splitlines()
    remote_branches = [
        line.split("\t", 1)[1].removeprefix("refs/heads/")
        for line in run_read(["git", "ls-remote", "--heads", "origin"], cwd=cwd).splitlines()
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
        if (released_claim_id(comment.get("body") or "") == comment_id
                or first == [f"Released by {parsed[0]['worker']}"]):
            return parsed[0]["branch"]
    raise ValueError("Resume source claim has not been released by its author")


def handoff_pr_numbers(text: str, *, issue_repo: str, target_repo: str) -> set[int]:
    qualified = rf"(?:https://github\.com/{re.escape(target_repo)}/pull/|(?<![\w/]){re.escape(target_repo)}#)([1-9]\d*)(?!\d)"
    numbers = {int(m.group(1)) for m in re.finditer(qualified, text, re.IGNORECASE)}
    if issue_repo.casefold() == target_repo.casefold():
        numbers.update(int(m.group(1)) for m in re.finditer(r"(?<![\w/#])#([1-9]\d*)(?!\d)", text))
    return numbers


def refresh_handoff(
    comments: list[dict[str, Any]], source_id: int, handoff_id: int,
    pulls: list[dict[str, Any]], target_number: int, *, issue_repo: str,
    issue_number: int, target_repo: str,
) -> set[str]:
    """Bind retained PR identities to the exact author-released handoff."""
    source_branch = retained_branch(comments, source_id)
    source = next(c for c in comments if c.get("id") == source_id)
    author = source["user"]["login"]
    handoff = next((c for c in comments if c.get("id") == handoff_id), None)
    if handoff is None or (handoff.get("user") or {}).get("login") != author:
        raise ValueError("Refresh handoff must be a comment by the source claim author")
    source_index, handoff_index = comments.index(source), comments.index(handoff)
    if handoff_index <= source_index or not any(
        (c.get("user") or {}).get("login") == author
        and released_claim_id(c.get("body") or "") == source_id
        for c in comments[source_index + 1:handoff_index + 1]
    ):
        raise ValueError("Refresh requires an exact source-claim release before or in the handoff")

    handoff_text = handoff.get("body") or ""
    source_record = records(source["body"])[0]
    standalone = (handoff_text.splitlines()[:1] == [f"Handoff from {source_record['worker']}"]
                  and re.search(rf"\bclaim {source_id}(?!\d)", handoff_text)
                  and re.search(rf"(?<![\w-]){re.escape(source_record['session'])}(?![\w-])", handoff_text)
                  and not records(handoff_text))
    if not (released_claim_id(handoff_text) == source_id or standalone):
        raise ValueError("Refresh handoff must identify the exact released source claim")
    empty = {"local_branches": [], "remote_branches": [], "worktrees": [], "sessions": []}
    permitted = {source_branch}
    target_found = False
    named = handoff_pr_numbers(handoff_text, issue_repo=issue_repo, target_repo=target_repo)
    for pull in pulls:
        branch = (pull.get("head") or {}).get("ref", "")
        if pull["number"] not in named:
            continue
        if pull.get("state") != "open" and not (pull.get("state") == "closed" and pull.get("merged_at")):
            continue
        if (pull.get("user") or {}).get("login") != author:
            continue
        if ((pull.get("head") or {}).get("repo") or {}).get("full_name", "").casefold() != target_repo.casefold():
            continue
        if ((pull.get("base") or {}).get("repo") or {}).get("full_name", "").casefold() != target_repo.casefold():
            continue
        # Classify only the issue reference, independently of branch evidence
        # or lifecycle state (open/merged admission was checked above).
        if not artifact_evidence(empty, [{**pull, "head": {"ref": ""}, "state": "open"}], issue_number, {},
                                 own_record=False, repo=issue_repo, inventory_repo=target_repo):
            continue
        permitted.add(branch)
        target_found |= pull["number"] == target_number and pull.get("state") == "open"
    if not target_found:
        raise ValueError("Refresh target must be an open same-repository PR linked to the issue and attested in the released handoff")
    return permitted


def artifact_evidence(
    inventory: dict[str, Any], pulls: list[dict[str, Any]], number: int,
    claim: dict[str, str], *, own_record: bool, retained: str | None = None, repo: str = "",
    retained_branches: set[str] | None = None, retained_repo: str | None = None,
    inventory_repo: str | None = None,
) -> list[dict[str, Any]]:
    conflicts = []
    local_references = inventory_repo is None or inventory_repo.casefold() == repo.casefold()
    def permitted(branch: str) -> bool:
        return (own_record and branch == claim["branch"]) or branch == retained or branch in (retained_branches or set())

    for source in ("local_branches", "remote_branches"):
        for branch in inventory[source]:
            if local_references and references_issue(branch, number) and not permitted(branch):
                conflicts.append({"source": source, "branch": branch, "certainty": "current_or_stale"})
    for tree in inventory["worktrees"]:
        if local_references and references_issue(tree["branch"] + "/" + pathlib.Path(tree["path"]).name, number):
            if not permitted(tree["branch"]):
                conflicts.append({"source": "worktree", **tree, "certainty": "current_or_stale"})
    paths = {str(pathlib.Path(t["path"]).resolve()) for t in inventory["worktrees"]}
    retained_paths = {str(pathlib.Path(t["path"]).resolve()) for t in inventory["worktrees"]
                      if t["branch"] == retained or t["branch"] in (retained_branches or set())}
    for session in inventory["sessions"]:
        if session.get("sessionId") == claim["session"]:
            continue
        cwd = str(pathlib.Path(session.get("cwd") or "/").resolve())
        if cwd in retained_paths or (local_references and cwd in paths and references_issue((session.get("name") or "") + "/" + pathlib.Path(cwd).name, number)):
            conflicts.append({"source": "claude_session", "session": session.get("sessionId"),
                              "state": session.get("state") or session.get("status")})
    for pull in pulls:
        branch = (pull.get("head") or {}).get("ref", "")
        title = pull.get("title") or ""
        body = pull.get("body") or ""
        issue_url = rf"https://github\.com/{re.escape(repo)}/issues/{number}"
        # Only the explicitly unstarted follow-up line is contextual.
        # Other URLs, titles, branches and implementation references still hold.
        issue_reference = rf"(?:{re.escape(repo)}#{number}|{issue_url})(?!\d)"
        if local_references:
            issue_reference = rf"(?:#{number}|{issue_reference})(?!\d)"
        ownership_reference = (
            rf"(?i)(?<![\w])(?:__)?(?:refs?|fix(?:es|ed)?|clos(?:e|es|ed)|resolv(?:e|es|ed)|implement(?:s|ed|ing)?)"
            rf"(?:\*\*|__)?\s*:?(?:\*\*|__)?\s+(?:{issue_reference}|<{issue_url}(?!\d)[^>]*>|"
            rf"\[[^\]\n]+\]\({issue_url}(?!\d)[^\n)]*\))"
        )
        ownership_body = "\n".join(
            line for line in body.splitlines()
            if not (line.startswith("Code follow-ups recorded without starting implementation:")
                    and not re.search(ownership_reference, line))
        )
        explicit_url = bool(repo and re.search(issue_url + r"(?!\d)", title + "\n" + ownership_body, re.IGNORECASE))
        linked = bool(re.search(ownership_reference, body))
        titled = local_references and bool(re.search(rf"(?<![\w/])#{number}(?!\d)", title))
        if explicit_url or linked or titled or (local_references and references_issue(branch, number)):
            if pull.get("state") == "closed":
                # Closed PRs are not open ownership evidence. Preserve any
                # unaccounted local/remote artifacts even for nonnumeric names.
                present = branch in inventory["local_branches"] or branch in inventory["remote_branches"] or any(
                    tree["branch"] == branch for tree in inventory["worktrees"]
                )
                if present and not permitted(branch):
                    conflicts.append({"source": "closed_pr_artifacts", "number": pull["number"], "branch": branch})
                continue
            same_repo = not retained_repo or all(
                ((pull.get(side) or {}).get("repo") or {}).get("full_name", "").casefold() == retained_repo.casefold()
                for side in ("head", "base")
            )
            if not permitted(branch) or not same_repo:
                conflicts.append({"source": "open_pr", "number": pull["number"], "branch": branch})
    return conflicts
