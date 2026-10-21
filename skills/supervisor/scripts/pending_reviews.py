#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Discover Supervisor review requests; --apply runs and posts, never weighs or readies."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn
from collections import Counter
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "skills/github/scripts"))
sys.path.insert(0, str(ROOT / "skills/model-review/scripts"))
from skills.github.scripts import github_open_prs
from github_api import redact_string
from github_direction_next import issue_references
from github_identity import OWN_USER_OPT_IN, automation_login, configured_bot_logins
from github_plan_claim import exact_released_sources, live_lines, records
from github_plan_release import effective_comments
from github_read import (GitHubReader, GitHubReadError, GitHubReadShapeError,
                         automation_only_gh_prefix_args, resolve_repo_from_remote)
from ready_handoff import (SUPERVISOR_REPORT_HEADING as REPORT_HEADING, raw_receipt, reported_review,
                           review_prose_lines, review_receipt_head, supervisor_review_request)
from review_worktree import is_review_scratch
from review_notifications import NotSent

REPO = re.compile(r"[\w.-]+/[\w.-]+", re.ASCII)
COMMENT_LINK = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)#issuecomment-(\d+)")
HEAD = re.compile(r"\b(?:head(?: SHA)?|commit)\s*[:=]?\s*`?([0-9a-f]{7,40})\b", re.I)


class ReviewError(ValueError):
    """An unsafe or ambiguous binding; no alternative launch or identity is tried."""


class OtherRun(ReviewError):
    """A trusted branch claim belongs to a session outside this run."""


class ReviewFailure(ReviewError):
    def __init__(self, metadata):
        super().__init__(f"review failed; no result posted: {json.dumps(metadata)}")
        self.metadata = metadata


class PostError(ReviewError):
    def __init__(self, message, *, phase="post_unknown", payload=None):
        super().__init__(message)
        self.phase, self.payload = phase, payload


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL, **kwargs)


def filesystem_failure(error: OSError, path: Path) -> NoReturn:
    if error.errno not in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL}:
        raise error
    raise ReviewError(f"unsupported filesystem operation at {path}: {error}; run and evidence directories need "
                      "a local POSIX filesystem with hard links, flock and directory fsync. Preserve existing "
                      "journals/results when relocating; reconcile any spent launch before apply. No alternate path was tried") from error


@contextmanager
def run_lock(ledger):
    """A read-only directory lock binds the run across registry/evidence replacements."""
    directory = ledger.resolve().parent
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BlockingIOError(error.errno, f"pending review sweep already holds run directory {directory}") from error
        except OSError as error:
            filesystem_failure(error, directory)
        yield
    finally:
        os.close(descriptor)


def private_evidence_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_mode & 0o077:
        raise ReviewError(f"evidence directory must be private (mode 0700): {path}; restrict this directory's permissions, retain its journals and rerun")


def atomic_text(path, content, *, exclusive=False):
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)  # Atomic publication that never replaces a spent reservation.
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        filesystem_failure(error, path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def sweep_lock(ledger, evidence, *, apply):
    # Discovery reads remote state and journals; it needs neither lock nor writes.
    if not apply:
        yield
        return
    with run_lock(ledger):
        private_evidence_dir(evidence)
        descriptor = os.open(evidence / "pending-reviews.lock", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                filesystem_failure(error, evidence)
            yield


def review_entries(path, *, errors=None):
    entries = json.loads(path.read_text())
    if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
        raise ReviewError("ledger must be an array of session objects; supply a corrected ledger or labeled valid derivative")
    counts = Counter(e["session_id"] for e in entries if e.get("supervisor_owned") is True
                     and isinstance(e.get("session_id"), str))
    selected = []
    for index, entry in enumerate(entries):
        if entry.get("supervisor_owned") is not True:
            continue
        if (not isinstance(entry.get("session_id"), str) or not entry["session_id"].strip()
                or counts[entry["session_id"]] != 1 or entry.get("harness") not in ("codex", "claude")
                or not isinstance(entry.get("repository"), str) or not REPO.fullmatch(entry["repository"])):
            reason = "Supervisor entry needs a unique session_id, native harness and OWNER/REPO"
            if errors is None:
                raise ReviewError(reason)
            errors.append({"ledger_index": index, "error": reason})
            continue
        if isinstance(entry.get("transcript"), str) and entry["transcript"].strip():
            transcript = Path(entry["transcript"]).expanduser()
            entry = {**entry, "transcript": str((path.parent / transcript).resolve())}
        selected.append(entry)
    return selected


def trusted(comment, logins):
    return (comment.get("user") or {}).get("login", "").casefold() in logins


def latest_request(comments, logins):
    asks = [c for c in comments if trusted(c, logins) and review_receipt_head(c.get("body") or "") is None
            and supervisor_review_request(c.get("body") or "")
            and raw_receipt(c.get("body") or "") is None and not reported_review(c.get("body") or "")]
    return max(asks, key=lambda c: (c["created_at"], c["id"]), default=None)


def comment_binding(comment):
    """Only authored content and identity invalidate a review, not reactions/API shape."""
    if comment is None:
        return None
    return (comment["id"], comment.get("body"), comment.get("updated_at") or comment["created_at"],
            (comment.get("user") or {}).get("login", "").casefold())


def completion_record(comments, request, head, logins) -> dict[str, Any] | None:
    try:
        bind_head(request, head)
    except ReviewError:
        return None
    weighed: dict[str, Any] | None = None
    supervisor: dict[str, Any] | None = None
    reported: dict[str, Any] | None = None
    def key(item):
        return item["created_at"], item["id"]
    def report_key(item):
        return item.get("updated_at") or item["created_at"], item["id"]
    for comment in comments:
        if not trusted(comment, logins):
            continue
        body = comment.get("body") or ""
        receipt = raw_receipt(body)
        if receipt and all(receipt.get(k) == v for k, v in receipt_for(request, head).items()):
            edited = (receipt.get("request_updated_at") is not None
                      and receipt["request_updated_at"] != (request.get("updated_at") or request["created_at"]))
            if supervisor is None or report_key(comment) > report_key(supervisor["comment"]):
                supervisor = {"kind": "supervisor", "comment": comment, "receipt": receipt,
                              "request_edited_after_review": edited}
        # A parsed machine receipt must match above; a different request/head
        # cannot become an unbound manual report for this request.
        if receipt is None and reported_review(body) and key(comment) >= key(request):
            try:
                bind_head(comment, head)
            except ReviewError:
                pass
            else:
                if reported is None or report_key(comment) > report_key(reported["comment"]):
                    reported = {"kind": "reported", "comment": comment}
        if (review_receipt_head(body) == head and key(comment) >= key(request)
                and (weighed is None or key(comment) > key(weighed["comment"]))):
            weighed = {"kind": "weighed", "comment": comment}
    report = supervisor if supervisor and (reported is None or report_key(supervisor["comment"]) >= report_key(reported["comment"])) else reported
    after_edit = lambda c: c["created_at"] >= (request.get("updated_at") or request["created_at"])
    if report:
        report["acknowledged"] = (weighed is not None and after_edit(weighed["comment"])
                                  and key(weighed["comment"]) >= report_key(report["comment"]))
        report["report_changed_after_disposition"] = weighed is not None and key(weighed["comment"]) < report_key(report["comment"])
        if supervisor and report is not supervisor:
            report["receipt"] = supervisor["receipt"]  # Preserve the original author identity.
            report["request_edited_after_review"] = supervisor.get("request_edited_after_review", False)
        return report
    if weighed:
        # A disposition of some other review is not an acknowledgement of this
        # delegation. Explicit acknowledgement only settles the queue item.
        acknowledgements = [c for c in comments if trusted(c, logins)
                            and review_receipt_head(c.get("body") or "") == head
                            and key(c) >= key(request) and after_edit(c)
                            and f"Supervisor request acknowledged: {request['id']}" in review_prose_lines(c.get("body") or "")]
        if acknowledgements:
            return {"kind": "acknowledged", "comment": max(acknowledgements, key=key), "acknowledged": True}
        return {**weighed, "kind": "weighed_without_review", "acknowledged": False}
    return None


def completion_report(record, io, repo, pr, entries, logins, request, evidence, history=None):
    receipt = record.get("receipt", {})
    comment = record["comment"]
    updated = comment.get("updated_at") or comment["created_at"]
    journal = evidence / f"{journal_key(repo, pr['number'], request, pr['head']['sha'])}.json"
    history = history if history is not None else local_history(evidence, repo, pr["number"], request, pr["head"]["sha"], comment)
    # Posted identity is historical evidence. Retirement must neither erase it
    # nor replace it with a successor. Legacy reports may recover it locally.
    sessions = {s.get("author_session") for s in [*history["sources"], receipt] if s.get("author_session")}
    providers = {s.get("author_provider") for s in [*history["sources"], receipt] if s.get("author_provider")}
    session = next(iter(sessions)) if len(sessions) == 1 else None
    author = next(iter(providers)) if len(providers) == 1 else None
    error = history["error"]
    if len(sessions) > 1 or len(providers) > 1:
        error = "conflicting retained review authors; reconcile history, never notify a successor"
    if (session is None or author is None) and not error:
        try:
            bound_session, bound_author = bind_session(io, repo, {pr["head"]["ref"]}, entries, logins, request, pr)
            if session is not None and bound_session != session:
                raise ReviewError("posted review author cannot be replaced by a successor")
            session, author = bound_session, bound_author
        except (ValueError, GitHubReadError, GitHubReadShapeError) as reason:
            error = str(reason)
    confirmed = local_completion(evidence, repo, pr["number"], request, pr["head"]["sha"], session, author)
    edited = record.get("request_edited_after_review", False)
    acknowledged = record.get("acknowledged", False)
    status = {"supervisor": "already_completed", "reported": "already_weighed" if acknowledged else "reported_review",
              "weighed_without_review": "weighed_without_review", "acknowledged": "request_acknowledged"}
    return {**journal_report(journal), "status": status[record["kind"]],
            "completion_kind": record["kind"], "requested_provider_confirmed": confirmed,
            "request_edited_after_review": edited,
            "needs_attention": record["kind"] == "weighed_without_review" or (not acknowledged and (not confirmed or edited or record.get("report_changed_after_disposition", False))),
            "head": pr["head"]["sha"], "request": request["id"],
            "author_session": session, "author_provider": author, "notification_error": error,
            "notification_needed": record["kind"] in {"supervisor", "reported"} and not acknowledged,
            "completion_id": comment["id"],
            "completion_comment": comment.get("html_url") or comment.get("url") or comment["id"],
            "completion_updated_at": updated}


def notification_path(evidence, repo, number, comment_id, updated):
    version = hashlib.sha256(json.dumps(updated).encode()).hexdigest()
    return evidence / f"{notification_prefix(repo, number, comment_id)}{version}.json"


def notification_prefix(repo, number, comment_id):
    return f"notification-{repo.casefold().replace('/', '~')}-{number}-{comment_id}-"


def local_history(evidence, repo, number, request, head, comment):
    """Historical provenance is independent of live permission to deliver."""
    paid = evidence / f"{journal_key(repo, number, request, head)}.json"
    current = notification_path(evidence, repo, number, comment["id"], comment.get("updated_at") or comment["created_at"])
    paths = sorted(evidence.glob(notification_prefix(repo, number, comment["id"]) + "*.json"))
    result = {"exists": paid.exists() or bool(paths), "sources": [], "error": None}
    errors = []
    for path in [paid, *paths]:
        try:
            state = notification_state(path)
            if not state and not path.exists():
                continue
            if path == paid:
                if state.get("head") != head or state.get("request") != request["id"]:
                    raise ReviewError("retained paid review binding differs from its journal")
            else:
                if (state.get("repository") != repo.casefold() or state.get("pr") != number
                        or state.get("completion_id") != comment["id"]
                        or notification_path(evidence, repo, number, comment["id"], state.get("completion_updated_at")) != path):
                    raise ReviewError("retained notification binding differs from its journal")
                if (not isinstance(state.get("author_session"), str) or not state["author_session"]
                        or state.get("author_provider") not in {"openai", "anthropic"}):
                    raise ReviewError("retained notification author is missing or invalid; never rebind")
            result["sources"].append(state)
            if path != paid and path != current and state.get("phase") not in {"sent", "not_sent"}:
                raise ReviewError(state.get("error") or "prior report version has an unknown notification outcome; never replay")
        except (ValueError, OSError) as error:
            errors.append(f"{path}: {error}")
    result["error"] = "; ".join(errors) or None
    return result


def retained_repositories(evidence):
    """Recover owned work after ledger retirement, verifying legacy paid keys."""
    repos, errors = set(), []
    for path in sorted(evidence.glob("*.json")):
        notification = re.fullmatch(r"notification-([\w.-]+~[\w.-]+)-(\d+)-(\d+)-[0-9a-f]{64}\.json", path.name, re.ASCII)
        if notification:
            repos.add(notification[1].replace("~", "/"))
            continue
        if not re.fullmatch(r".+-\d+-[0-9a-f]{64}\.json", path.name):
            continue
        try:
            state = notification_state(path)
            candidates = {state.get("repository"), *(s.get("repository") for s in state.get("prompt_sources", []))} - {None}
            number = int(path.stem.rsplit("-", 2)[1])
            matches = {repo.casefold() for repo in candidates if isinstance(repo, str) and REPO.fullmatch(repo)
                       and path.stem == journal_key(repo, number, {"id": state["request"]}, state["head"])}
            if len(matches) != 1:
                raise ReviewError("cannot recover a unique repository from retained paid journal")
            repos.update(matches)
        except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
            errors.append({"status": "refused", "journal": str(path), "error": str(error), "needs_attention": True})
    return repos, errors


def notification_state(path):
    if not path.exists():
        return {}
    state = json.loads(path.read_text())
    if not isinstance(state, dict):
        raise ReviewError("notification journal is not an object; retain it and reconcile delivery")
    return state


def receipt_for(request, head):
    return {"request": request["id"], "head": head}


def journal_key(repo, number, request, head):
    binding = json.dumps(receipt_for(request, head), sort_keys=True)
    return f"{repo.replace('/', '-')}-{number}-" + hashlib.sha256(binding.encode()).hexdigest()


def journal_report(journal: Path) -> dict[str, Any]:
    if not journal.exists():
        return {}
    try:
        prior = json.loads(journal.read_text())
        if not isinstance(prior, dict):
            raise ReviewError("journal is not an object")
    except (ValueError, OSError) as error:
        prior = {"phase": "unreadable", "error": str(error)}
    failure = prior.get("review_failure")
    failure = failure if isinstance(failure, dict) else {}
    return {"journal": str(journal), "journal_phase": prior.get("phase"),
            "review_phase": failure.get("phase"), "review_outcome": failure.get("outcome"),
            "error": prior.get("error"), "evidence": prior.get("evidence"),
            "response_evidence": prior.get("response_evidence"), "prompt_evidence": prior.get("prompt_evidence")}


def local_completion(evidence, repo, number, request, head, session, author):
    """A shared bot comment alone cannot prove that requested inference completed."""
    key = journal_key(repo, number, request, head)
    try:
        state = json.loads((evidence / f"{key}.json").read_text())
        review = state.get("review") or {}
        provider = {"openai": "anthropic", "anthropic": "openai"}.get(author)
        return (state.get("phase") in {"review_completed", "post_blocked", "post_pending", "post_attempted", "post_unknown", "posted"}
                and state.get("head") == head and state.get("request") == request["id"]
                and state.get("author_session") == session and session is not None
                and state.get("author_provider") == author and provider is not None
                and review.get("ok") is True and review.get("provider") == provider
                and bool((evidence / f"{key}.md").read_text().strip()))
    except (ValueError, OSError, AttributeError, TypeError):
        return False


def inline_prompt(body):
    # The surrounding request is context, not a substitute for a missing prompt.
    heading = re.search(r"(?im)(?:^|[ \t])(?:Review )?Prompt:[ \t]*\n+", body)
    if heading:
        tail = body[heading.end():]
        opening = re.match(r" {0,3}(`{3,}|~{3,})[^\n]*\n", tail)
        if opening:
            fence = opening[1]
            lines = tail[opening.end():].splitlines(keepends=True)
            for index, line in enumerate(lines):
                if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*", line.rstrip("\r\n")):
                    text = "".join(lines[:index]).strip()
                    if not text:
                        raise ReviewError("empty fenced prompt; supply the review scope")
                    return text if COMMENT_LINK.sub("", text).strip() else None
            raise ReviewError("unclosed fenced prompt; close it with the same character and at least the opening length")
    match = re.search(r"(?ims)(?:^|[ \t])(?:Review )?Prompt:[ \t]*(?:\n+)?(.*)", body)
    if match:
        text = re.sub(r"(?s)<!--.*?-->", "", match[1]).strip()
        if text:
            return text if COMMENT_LINK.sub("", text).strip() else None
    # Explicit prompt sections win; otherwise resolve a linked prompt before
    # treating quoted context (or legacy direct prose) as the review scope.
    if COMMENT_LINK.search(body):
        return None
    quoted = "\n".join(line[1:].lstrip() for line in body.splitlines() if line.startswith(">"))
    if quoted.strip():
        return quoted.strip()
    if (re.search(r"\bread.only\b", body, re.I) and re.search(r"\bread\s+", body, re.I)
            and re.search(r"\bcheck\s+", body, re.I)):
        return re.sub(r"(?s)<!--.*?-->", "", body).strip()
    return None


def resolve_prompt(io, repo, request, logins):
    current, seen, sources = request, set(), []
    for _ in range(8):
        key = (repo, current["id"])
        if key in seen:
            raise ReviewError("linked prompt cycle")
        seen.add(key)
        if not trusted(current, logins):
            raise ReviewError("untrusted linked prompt")
        sources.append((repo, current))
        prompt = inline_prompt(current["body"])
        if prompt:
            context = re.sub(r"(?s)<!--.*?-->", "", request["body"]).strip()
            return context + "\n\nReview prompt:\n" + prompt, sources
        links = list(set(COMMENT_LINK.findall(current["body"])))
        if len(links) != 1 or links[0][0].casefold() != repo.casefold():
            raise ReviewError("missing or ambiguous prompt; post Prompt: or one same-repository comment link")
        linked_repo, number, ident = links[0]
        current = io.get_json(f"/repos/{linked_repo}/issues/comments/{ident}", step="linked_prompt")
        expected = f"https://api.github.com/repos/{linked_repo}/issues/{number}".casefold()
        if current.get("issue_url", "").casefold() != expected or current.get("id") != int(ident):
            raise ReviewError("linked prompt identity mismatch")
    raise ReviewError("linked prompt depth exceeded")


def declared_heads(request):
    context = re.split(r"(?im)(?:^|[ \t])(?:Review )?Prompt:|^>|^```", request["body"], maxsplit=1)[0]
    explicit = re.findall(r"(?im)^Head(?: SHA)?:\s*`?([0-9a-f]{7,40})\b", context)
    heads = explicit or HEAD.findall(context)
    if not heads:
        # Legacy prose may say "on SHA"; short numeric dates and counts are not
        # head declarations. Abbreviated legacy hashes require code formatting.
        legacy = re.findall(r"\b(?:on|at)\s+(?:`([0-9a-f]{7,40})`|([0-9a-f]{40})\b)", context, re.I)
        heads = [short or full for short, full in legacy]
    return [value.lower() for value in heads]


def bind_head(request, head):
    heads = declared_heads(request)
    if not heads or any(not head.startswith(value) for value in heads):
        raise ReviewError("request head missing or moved; renew the request with Head: FULL_SHA")


def stale_completion(comments, request, current_head, logins):
    # Resolve abbreviations only against full heads recorded in trusted evidence.
    heads = set(declared_heads(request))
    for comment in comments:
        if not trusted(comment, logins):
            continue
        body = comment.get("body") or ""
        receipt = raw_receipt(body)
        if receipt:
            heads.add(receipt["head"])
        weighed_head = review_receipt_head(body)
        if weighed_head:
            heads.add(weighed_head)
        if reported_review(body):
            heads.update(declared_heads(comment))
    completed = []
    for head in sorted(heads):
        if len(head) != 40 or head == current_head:
            continue
        try:
            bind_head(request, head)
        except ReviewError:
            continue
        record = completion_record(comments, request, head, logins)
        if record:
            completed.append((head, record))
    if len(completed) > 1:
        raise ReviewError("ambiguous historical review head; record the exact completed head in the request")
    return completed[0] if completed else None


def live_claim_comments(comments, branches):
    comments = sorted(effective_comments(comments), key=lambda c: (c["created_at"], c["id"]))
    cleaned = []
    for comment in comments:
        body = comment.get("body") or ""
        for line in live_lines(body):
            try:
                records(line)
            except ValueError:
                match = re.fullmatch(r"\s*<!-- github-plan:claim (.*?) -->\s*", line)
                raw = json.loads(match[1]) if match else None
                if (isinstance(raw, dict) and isinstance(raw.get("branch"), str)
                        and raw["branch"] and raw["branch"] not in branches):
                    body = body.replace(line, "", 1)
                    continue
                raise
        cleaned.append({**comment, "body": body})
    # The shared parser verifies author, exact source and release chronology;
    # invalid abandoned-release receipts restore ownership before this check.
    ended = {cleaned[index]["id"] for index, _, _ in exact_released_sources(cleaned)}
    return [comment for comment in cleaned if comment["id"] not in ended]


def bind_session(io, repo, branches, entries, logins, request, pr, *, refresh=False):
    candidates = {entry["session_id"]: entry for entry in entries
                  if entry.get("repository", "").casefold() == repo.casefold()
                  and entry.get("supervisor_owned") is True}
    owned_issues = {entry.get("issue") for entry in candidates.values()
              if isinstance(entry.get("issue"), int) and not isinstance(entry["issue"], bool) and entry["issue"] > 0}
    # A brief may name a parent before its worker creates the implementation issue.
    # Linked issues locate claims; the trusted claim must still bind branch and session.
    issues = set(owned_issues)
    issues.update(ref["number"] for ref in issue_references(pr.get("body") or "", repo=repo)
                  if ref["repo"].casefold() == repo.casefold())
    matches, outside = {}, set()
    for issue in sorted(issues):
        comments = io.paged_json(f"/repos/{repo}/issues/{issue}/comments",
                                 step_prefix="verify_author_claims" if refresh else "author_claims")
        comments = live_claim_comments([c for c in comments if trusted(c, logins)], branches)
        for comment in comments:
            for claim in records(comment["body"]):
                if claim["branch"] in branches:
                    if claim["session"] in candidates:
                        matches[claim["session"]] = candidates[claim["session"]]
                    else:
                        outside.add(claim["session"])
    explicit = re.findall(r"(?im)^Author session:\s*`?([^`\s]+)`?\s*$", request["body"])
    if len(explicit) > 1:
        raise ReviewError("name exactly one Author session: from its branch claim")
    if explicit and explicit[0] in outside and explicit[0] not in candidates:
        raise OtherRun("explicit author matches a trusted branch claim outside this run")
    if matches and outside and not explicit:
        raise ReviewError("branch claims include other runs; name Author session: to resolve ownership")
    if not matches and outside and (not explicit or explicit[0] not in candidates):
        raise OtherRun("trusted PR branch claim belongs to a session outside this ledger")
    if explicit:
        if len(explicit) != 1 or explicit[0] not in matches:
            raise ReviewError("author session does not match a recorded branch claim")
        matches = {explicit[0]: matches[explicit[0]]}
    if len(matches) != 1:
        raise ReviewError("missing or ambiguous author session; name Author session: from the branch claim")
    entry = next(iter(matches.values()))
    author = entry.get("author_provider") or {"codex": "openai", "claude": "anthropic"}[entry["harness"]]
    if author not in {"openai", "anthropic"}:
        raise ReviewError("author provider is unsupported")
    return entry["session_id"], author


def git(path, *args):
    result = run(["git", "-C", str(path), *args], timeout=60)
    if result.returncode:
        raise ReviewError(f"git {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def check_worktree(path, repo, branch, head):
    if resolve_repo_from_remote(path, "origin").casefold() != repo.casefold():
        raise ReviewError("worktree repository mismatch")
    if git(path, "rev-parse", "HEAD") != head or git(path, "symbolic-ref", "--short", "HEAD") != branch:
        raise ReviewError("worktree head or branch moved")
    git(path, "diff", "--quiet", "HEAD", "--")
    names = git(path, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    if any(name and not is_review_scratch(name) for name in names):
        raise ReviewError("dirty worktree (including untracked project files)")


def registered_worktrees(checkout):
    raw = git(checkout, "worktree", "list", "--porcelain", "-z")
    trees = []
    for block in raw.split("\0\0"):
        fields = {name: value for line in block.split("\0") if line
                  for name, _, value in [line.partition(" ")]}
        if fields.get("branch") and fields.get("HEAD"):
            trees.append((Path(fields["worktree"]), fields["branch"].removeprefix("refs/heads/"), fields["HEAD"]))
    return trees


def find_worktree(checkout, repo, branch, head, preferred=None):
    candidates = [(path, ref) for path, ref, sha in registered_worktrees(checkout) if sha == head]
    if preferred:
        candidates = [(path, ref) for path, ref in candidates if path.resolve() == preferred.resolve()]
    else:
        matching_branch = [(path, ref) for path, ref in candidates if ref == branch]
        candidates = matching_branch or candidates
    if len(candidates) != 1:
        raise ReviewError("no unique local worktree at the PR head; name Worktree: /absolute/path")
    path, worktree_branch = candidates[0]
    check_worktree(path, repo, worktree_branch, head)
    return path


class IO:
    def __init__(self):
        self.reader = GitHubReader(gh_prefix_args=automation_only_gh_prefix_args(), cache_revalidate=True)
        self.claims = {}

    def open_pulls(self, repos):
        return github_open_prs.scan(self.reader, repos)

    def get_json(self, path, **kwargs):
        return self.reader.get_json(path, **kwargs)

    def paged_json(self, path, **kwargs):
        if kwargs.get("step_prefix") in {"author_claims", "verify_author_claims"}:
            if path not in self.claims or kwargs["step_prefix"] == "verify_author_claims":
                self.claims[path] = self.reader.paged_json(path, **kwargs)
            return self.claims[path]
        return self.reader.paged_json(path, **kwargs)

    @staticmethod
    def notify(entry, message):
        from review_notifications import send
        return send(entry, message)

    @staticmethod
    def review(author, worktree, prompt, output):
        provider = "anthropic" if author == "openai" else "openai"
        result = run(["uv", "run", str(ROOT / "skills/model-review/scripts/review_with_model.py"),
                      "run", "--author-provider", author, "--provider", provider, "--repo", str(worktree),
                      "--prompt-file", str(prompt), "--out", str(output)])
        try:
            metadata = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ReviewError("review returned invalid metadata: " + redact_string(result.stderr.strip())) from error
        if not isinstance(metadata, dict):
            raise ReviewError("review metadata is not an object")
        if (result.returncode or metadata.get("ok") is not True or metadata.get("provider") != provider
                or metadata.get("outcome") in {"refused", "incomplete", "unavailable"}
                or metadata.get("response_file") != str(output)
                or not output.is_file() or not output.read_text().strip()):
            raise ReviewFailure(metadata)
        return metadata

    @staticmethod
    def post(repo, number, body):
        result = run(["uv", "run", str(ROOT / "skills/github/scripts/gh-pr.py"), "--repo", repo,
                      "comment", str(number), "--body-file", str(body)], timeout=180,
                     env={key: value for key, value in os.environ.items() if key != OWN_USER_OPT_IN})
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise PostError("posting returned invalid metadata; do not replay: " + redact_string(result.stderr.strip())) from error
        if (not isinstance(payload, dict) or result.returncode or payload.get("ok") is not True
                or payload.get("outcome_certainty") not in {"confirmed", "reconciled_applied"}):
            certainty = payload.get("outcome_certainty") if isinstance(payload, dict) else None
            phase = "post_failed" if certainty in {"confirmed_not_applied", "reconciled_not_applied"} else "post_unknown"
            raise PostError(f"posting not confirmed; do not replay: {json.dumps(payload)}", phase=phase, payload=payload)
        return payload


def verify_inputs(io, repo, number, pr, worktree, worktree_branch, request, sources, logins):
    head = pr["head"]["sha"]
    check_worktree(worktree, repo, worktree_branch, head)
    fresh = io.get_json(f"/repos/{repo}/pulls/{number}", step="verify_review_pr")
    if (fresh["state"] != "open" or any(fresh["head"].get(k) != pr["head"].get(k) for k in ("sha", "ref"))
            or (fresh["head"].get("repo") or {}).get("full_name", "").casefold() != repo.casefold()):
        raise ReviewError("PR head moved; renew the request")
    for source_repo, source in sources:
        current = io.get_json(f"/repos/{source_repo}/issues/comments/{source['id']}", step="verify_review_prompt")
        if comment_binding(current) != comment_binding(source):
            raise ReviewError("review request or linked prompt changed")
    comments = io.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="verify_review_comments")
    if comment_binding(latest_request(comments, logins)) != comment_binding(request):
        raise ReviewError("review request superseded")
    return completion_record(comments, request, head, logins)


def dispatch_review(io, repo, number, entries, checkout, logins, evidence, apply=False):
    path = f"/repos/{repo}/pulls/{number}"
    pr = io.get_json(path, step="review_pr")
    if pr["state"] != "open":
        return {"status": "closed"}
    comments_path = f"/repos/{repo}/issues/{number}/comments"
    comments = io.paged_json(comments_path, step_prefix="review_comments")
    request = latest_request(comments, logins)
    if request is None:
        return {"status": "no_request"}
    if (pr["head"].get("repo") or {}).get("full_name", "").casefold() != repo.casefold():
        raise ReviewError("fork head has no verified local binding")
    head, branch = pr["head"]["sha"], pr["head"]["ref"]
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ReviewError("invalid PR head")
    completed = completion_record(comments, request, head, logins)
    old = stale_completion(comments, request, head, logins) if completed is None else None
    reviewed_head, completed = old if old else (head, completed)
    history = None

    def completed_result():
        historical_pr = {**pr, "head": {**pr["head"], "sha": reviewed_head}}
        result = completion_report(completed, io, repo, historical_pr, entries, logins, request, evidence, history)
        if reviewed_head != head:
            return {**result, "status": "completed_stale", "completion_status": result["status"],
                    "reviewed_head": reviewed_head, "current_head": head}
        return result

    if completed:
        comment = completed["comment"]
        history = local_history(evidence, repo, number, request, reviewed_head, comment)
        if history["exists"]:
            # This run's delivery history remains observable after retirement.
            # Foreign completions without local history retain the ownership gate.
            return completed_result()
    try:
        bind_session(io, repo, {branch}, entries, logins, request, pr)
    except OtherRun as error:
        if checkout is not None:
            same_head = {ref for _, ref, sha in registered_worktrees(checkout) if sha == head}
            try:
                bind_session(io, repo, {branch, *same_head}, entries, logins, request, pr)
            except OtherRun:
                pass
            else:
                raise ReviewError("same-head successor belongs to this run; name Author session: before review")
        return {"status": "not_owned", "head": head, "request": request["id"], "reason": str(error)}
    except (ValueError, GitHubReadError, GitHubReadShapeError):
        # Completion can still be reported without a live claim lookup. Pending
        # requests must establish ownership after selecting the exact worktree.
        pass
    if completed:
        return completed_result()
    if checkout is None:
        raise ReviewError("repository checkout missing; pass --checkout OWNER/REPO=/absolute/path")
    bind_head(request, head)
    prompt, sources = resolve_prompt(io, repo, request, logins)
    paths = re.findall(r"(?im)^Worktree:\s*`?([^`\n]+?)`?\s*$", request["body"])
    if len(paths) > 1 or (paths and not Path(paths[0]).is_absolute()):
        raise ReviewError("Worktree: must name one absolute registered path")
    worktree = find_worktree(checkout, repo, branch, head, Path(paths[0]) if paths else None)
    worktree_branch = git(worktree, "symbolic-ref", "--short", "HEAD")
    if worktree_branch != branch and not re.search(r"(?im)^Author session:", request["body"]):
        raise ReviewError("successor worktree requires explicit Author session: from its trusted branch claim")
    session, author = bind_session(io, repo, {branch, worktree_branch}, entries, logins, request, pr)
    report = {"status": "pending", "head": head, "request": request["id"], "author_session": session,
              "author_provider": author, "worktree": str(worktree)}
    key = journal_key(repo, number, request, head)
    journal = evidence / f"{key}.json"
    if not apply:
        retained = journal_report(journal)
        if retained:
            return {**report, **retained, "status": "blocked", "needs_attention": True,
                    "reason": "prior launch exists; reconcile retained evidence, never replay inference or an unknown write"}
        return report
    existing = verify_inputs(io, repo, number, pr, worktree, worktree_branch, request, sources, logins)
    if existing:
        return completion_report(existing, io, repo, pr, entries, logins, request, evidence)
    private_evidence_dir(evidence)
    prompt_file = evidence / f"{key}.prompt.txt"
    output = evidence / f"{key}.response.txt"
    launch = {**report, "repository": repo.casefold(), "pr": number,
              "prompt_evidence": str(prompt_file), "response_evidence": str(output), "prompt_sources": [
        {"repository": source_repo, "binding": comment_binding(source)} for source_repo, source in sources]}
    # Exclusive creation survives process interruption and unknown remote writes.
    # A later sweep reads remote completion first, then refuses a spent launch.
    try:
        atomic_text(journal, json.dumps({**launch, "phase": "launch_started"}, indent=2), exclusive=True)
    except FileExistsError as error:
        raise ReviewError(f"prior launch exists at {journal}; inspect its outcome, do not replay a refusal or unknown write") from error
    with tempfile.TemporaryDirectory(prefix=f"{repo.replace('/', '-')}-{number}-", dir=evidence) as scratch:
        folder = Path(scratch)
        atomic_text(prompt_file, prompt)
        atomic_text(output, "")  # The helper writes here directly; cleanup cannot discard paid output.
        try:
            metadata = io.review(author, worktree, prompt_file, output)
            # The adapter's declared-completion contract must include actual text.
            review = output.read_text().strip()
            if not review or metadata.get("ok") is not True:
                raise ReviewError("review completion missing or empty")
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            atomic_text(journal, json.dumps({**launch, "phase": "review_failed", "error": str(error),
                                            "review_failure": getattr(error, "metadata", None)}, indent=2))
            raise
        # Keep every completed review before any freshness check or remote write.
        # Changed inputs prevent posting, but cannot discard paid inference.
        result_file = evidence / f"{key}.md"
        atomic_text(result_file, review + "\n")
        retained = {**launch, "review": metadata, "evidence": str(result_file)}
        atomic_text(journal, json.dumps({**retained, "phase": "review_completed"}, indent=2))
        try:
            existing = verify_inputs(io, repo, number, pr, worktree, worktree_branch, request, sources, logins)
            current_author = bind_session(io, repo, {branch, worktree_branch}, entries, logins, request, pr, refresh=True)
            if current_author != (session, author):
                raise ReviewError("author branch claim changed; retain the completed result and resolve the current author before posting")
        except (GitHubReadError, GitHubReadShapeError, ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
            atomic_text(journal, json.dumps({**retained, "phase": "post_blocked", "error": str(error)}, indent=2))
            raise
        if existing and existing["kind"] != "weighed_without_review":
            return completion_report(existing, io, repo, pr, entries, logins, request, evidence)
        atomic_text(journal, json.dumps({**retained, "phase": "post_pending"}, indent=2))
        body = folder / "post.md"
        body.write_text(f"{REPORT_HEADING}\n\nHead: {head}\n"
                        f"Provider: {metadata['provider']}\nModel: {metadata.get('model', 'unknown')}\n"
                        f"Model source: {metadata.get('model_source', 'unknown')}\n"
                        f"Authoring session to notify: {session}\n\n"
                        "The author weighs these findings before ready.\n\n" + review + "\n\n"
                        "<!-- supervisor-review " + json.dumps({**receipt_for(request, head), "author_session": session, "author_provider": author,
                            "request_updated_at": request.get("updated_at") or request["created_at"]}, sort_keys=True) + " -->\n")
        atomic_text(journal, json.dumps({**retained, "phase": "post_attempted"}, indent=2))
        try:
            receipt = io.post(repo, number, body)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            atomic_text(journal, json.dumps({**retained, "phase": getattr(error, "phase", "post_unknown"),
                                           "error": str(error), "post": getattr(error, "payload", None)}, indent=2))
            raise
        atomic_text(journal, json.dumps({**retained, "phase": "posted", "post": receipt}, indent=2))
        posted = {**report, "status": "posted", "post": receipt, "evidence": str(result_file),
                  "notification_needed": True,
                  "completion_comment": receipt.get("url") or (receipt.get("comment") or {}).get("url")}
        try:
            comments = io.paged_json(comments_path, step_prefix="posted_review_comments")
            completed = completion_record(comments, request, head, logins)
            if not completed:
                raise ReviewError("posted review is not observable; retain the journal and reconcile delivery")
        except (GitHubReadError, GitHubReadShapeError, ValueError, OSError, KeyError, TypeError) as error:
            return {**posted, "notification_error": str(error), "needs_attention": True}
        return {**posted, **completion_report(completed, io, repo, pr, entries, logins, request, evidence),
                "status": "posted"}


def dispatch(io, repo, number, entries, checkout, logins, evidence, apply=False):
    report = dispatch_review(io, repo, number, entries, checkout, logins, evidence, apply)
    if not report.get("notification_needed"):
        return report
    try:
        if not report.get("completion_id") or not report.get("completion_updated_at"):
            raise ReviewError(report.get("notification_error") or "notification needs the posted comment identity and update timestamp")
        path = notification_path(evidence, repo, number, report.get("completion_id"), report.get("completion_updated_at"))
        report["notification_journal"] = str(path)
        prior = notification_state(path)
        if report.get("notification_error"):
            raise ReviewError(report["notification_error"])
        if prior.get("phase") == "sent":
            return {**report, "notification_status": "sent", "notification_error": None}
        if prior and prior.get("phase") != "not_sent":
            raise ReviewError(prior.get("error") or "notification outcome unknown; reconcile the retained attempt, never replay")
        matches = [e for e in entries if e.get("session_id") == report.get("author_session")]
        if (len(matches) != 1 or matches[0].get("supervisor_owned") is not True
                or matches[0].get("repository", "").casefold() != repo.casefold()
                or matches[0].get("harness") not in {"codex", "claude"}):
            raise ReviewError("notification author is not unique and verified in this repository's ledger")
        entry = matches[0]
        if not apply:
            return {**report, "notification_status": "blocked" if prior.get("needs_attention") else "pending",
                    "needs_attention": report.get("needs_attention", False) or prior.get("needs_attention", False),
                    "notification_error": prior.get("error")}
        private_evidence_dir(evidence)
        message = (f"Supervisor review posted for {repo}#{number}. "
                   f"Head: {report.get('reviewed_head') or report['head']}. "
                   f"Read {report['completion_comment']}, weigh the findings, and continue your brief. "
                   "This notification is not a weighed review or a ready handoff.")
        state = {"phase": "attempted", "author_session": entry["session_id"],
                 "author_provider": report["author_provider"], "repository": repo.casefold(), "pr": number,
                 "completion_id": report["completion_id"], "completion_updated_at": report["completion_updated_at"]}
        atomic_text(path, json.dumps(state), exclusive=not prior)
        try:
            io.notify(entry, message)
        except NotSent as error:
            atomic_text(path, json.dumps({**state, "phase": "not_sent", "error": str(error), "needs_attention": error.needs_attention}))
            raise
        except (ValueError, OSError, TypeError, KeyError, subprocess.SubprocessError) as error:
            atomic_text(path, json.dumps({**state, "phase": "failed", "error": str(error)}))
            raise
        atomic_text(path, json.dumps({**state, "phase": "sent"}))
        return {**report, "notification_status": "sent"}
    except NotSent as error:
        return {**report, "notification_status": "blocked" if error.needs_attention else "pending",
                "notification_error": str(error), "needs_attention": report.get("needs_attention", False) or error.needs_attention}
    except (ValueError, OSError, TypeError, KeyError, subprocess.SubprocessError) as error:
        return {**report, "notification_status": "blocked", "notification_error": str(error), "needs_attention": True}


def sweep(io, entries, checkouts, logins, evidence, apply=False):
    retained, reports = retained_repositories(evidence)
    repos = sorted(retained | {e["repository"].casefold() for e in entries if e.get("supervisor_owned") is True})
    checkouts = {repo.casefold(): path for repo, path in checkouts.items()}
    try:
        inventory = io.open_pulls(repos)
    except (GitHubReadError, GitHubReadShapeError, ValueError, KeyError, TypeError) as error:
        return reports + [{"repository": repo, "status": "refused", "error": str(error)} for repo in repos]
    for repo in repos:
        print(f"Discovering Supervisor reviews in {repo}", file=sys.stderr, flush=True)
        try:
            pulls = inventory[repo]
            if isinstance(pulls, Exception):
                raise pulls
        except (GitHubReadError, GitHubReadShapeError, ValueError, KeyError, TypeError) as error:
            reports.append({"repository": repo, "status": "refused", "error": str(error)})
            continue
        for pr in pulls:
            number = pr["number"]
            try:
                result = dispatch(io, repo, number, entries, checkouts.get(repo), logins, evidence, apply)
            except (GitHubReadError, GitHubReadShapeError, ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
                result = {"status": "refused", "error": str(error)}
            if result["status"] != "no_request":
                reports.append({"repository": repo, "pr": number, **result})
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--checkout", action="append", default=[], metavar="OWNER/REPO=PATH")
    parser.add_argument("--evidence-dir", type=Path, required=True,
                        help="Durable private state directory; retain journals/results, never use auto-pruned task evidence")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        ledger_errors = []
        entries = review_entries(args.ledger, errors=ledger_errors)
        checkouts = {}
        for mapping in args.checkout:
            repo, sep, path = mapping.partition("=")
            if not sep or not REPO.fullmatch(repo) or not Path(path).is_absolute() or repo.casefold() in checkouts:
                raise ReviewError("each --checkout needs a unique OWNER/REPO=/absolute/path")
            checkouts[repo.casefold()] = Path(path)
        logins = {v.casefold() for v in (*configured_bot_logins(), automation_login()) if v}
        if not logins:
            raise ReviewError("automation identities are not configured")
        # The private run directory stays stable when the ledger is replaced
        # atomically or callers choose different evidence directories.
        with sweep_lock(args.ledger, args.evidence_dir, apply=args.apply):
            reports = sweep(IO(), entries, checkouts, logins, args.evidence_dir, args.apply)
        attention = [r for r in reports if r.get("needs_attention")]
        print(json.dumps({"results": reports, "ledger_errors": ledger_errors, "attention": attention}, indent=2))
        return int(bool(ledger_errors or attention) or any(r["status"] == "refused" for r in reports))
    except (ValueError, OSError) as error:
        print(json.dumps({"error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
