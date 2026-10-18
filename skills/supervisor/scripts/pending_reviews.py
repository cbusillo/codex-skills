#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Discover Supervisor review requests; --apply runs and posts, never weighs or readies."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from collections import Counter
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "skills/github/scripts"))
sys.path.insert(0, str(ROOT / "skills/model-review/scripts"))
from github_api import redact_string
from github_direction_next import issue_references
from github_identity import automation_login, configured_bot_logins
from github_plan_claim import live_lines, records, without_operation_marker
from github_read import (GitHubReader, GitHubReadError, GitHubReadShapeError,
                         automation_only_gh_prefix_args, resolve_repo_from_remote)
from ready_handoff import review_receipt_head, review_record
from review_worktree import is_review_scratch

REPO = re.compile(r"[\w.-]+/[\w.-]+", re.ASCII)
COMMENT_LINK = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)#issuecomment-(\d+)")
HEAD = re.compile(r"\b(?:head(?: SHA)?|commit)\s*[:=]?\s*`?([0-9a-f]{7,40})\b", re.I)
RECEIPT = re.compile(r"<!-- supervisor-review (.*?) -->")


class ReviewError(ValueError):
    """An unsafe or ambiguous binding; no alternative launch or identity is tried."""


class OtherRun(ReviewError):
    """A trusted branch claim belongs to a session outside this run."""


class PostError(ReviewError):
    def __init__(self, message, *, phase="post_unknown", payload=None):
        super().__init__(message)
        self.phase, self.payload = phase, payload


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL, **kwargs)


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
        yield
    finally:
        os.close(descriptor)


@contextmanager
def sweep_lock(ledger, evidence, *, apply):
    # Discovery reads remote state and journals; it needs neither lock nor writes.
    if not apply:
        yield
        return
    with run_lock(ledger):
        evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (evidence / "pending-reviews.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
        selected.append(entry)
    return selected


def trusted(comment, logins):
    return (comment.get("user") or {}).get("login", "").casefold() in logins


def raw_receipt(body):
    # gh-pr appends a transport marker. Only the raw report's final marker
    # is a receipt; source snippets inside the review remain ordinary data.
    lines = without_operation_marker(body).strip().splitlines()
    match = RECEIPT.fullmatch(lines[-1]) if lines else None
    if match:
        try:
            value = json.loads(match[1])
        except json.JSONDecodeError:
            return None
        if (isinstance(value, dict) and type(value.get("request")) is int
                and isinstance(value.get("head"), str) and re.fullmatch(r"[0-9a-f]{40}", value["head"])):
            return value
    return None


def reported_review(body):
    first = body.strip().splitlines()[0].replace("**", "") if body.strip() else ""
    return (re.match(r"(?i)^Review of record[:,].*\brun by the Supervisor\b", first) is not None
            and not re.search(r"(?i)\b(?:pending|awaiting|waiting|not run)\b", first))


def latest_request(comments, logins):
    asks = [c for c in comments if trusted(c, logins) and review_record(c.get("body") or "") == "request"
            and raw_receipt(c.get("body") or "") is None and not reported_review(c.get("body") or "")]
    return max(asks, key=lambda c: (c["created_at"], c["id"]), default=None)


def comment_binding(comment):
    """Only authored content and identity invalidate a review, not reactions/API shape."""
    if comment is None:
        return None
    return (comment["id"], comment.get("body"), comment.get("updated_at") or comment["created_at"],
            (comment.get("user") or {}).get("login", "").casefold())


def completion_record(comments, request, head, logins):
    weighed, supervisor, reported = None, None, None
    def key(comment):
        return comment["created_at"], comment["id"]
    for comment in comments:
        if not trusted(comment, logins):
            continue
        body = comment.get("body") or ""
        receipt = raw_receipt(body)
        if receipt and all(receipt.get(k) == v for k, v in receipt_for(request, head).items()):
            edited = (receipt.get("request_updated_at") is not None
                      and receipt["request_updated_at"] != (request.get("updated_at") or request["created_at"]))
            if supervisor is None or key(comment) > key(supervisor["comment"]):
                supervisor = {"kind": "supervisor", "comment": comment, "receipt": receipt,
                              "request_edited_after_review": edited}
        if reported_review(body) and key(comment) >= key(request):
            try:
                bind_head(comment, head)
            except ReviewError:
                pass
            else:
                if reported is None or key(comment) > key(reported["comment"]):
                    reported = {"kind": "reported", "comment": comment}
        if (review_receipt_head(body) == head and key(comment) >= key(request)
                and (weighed is None or key(comment) > key(weighed["comment"]))):
            weighed = {"kind": "weighed", "comment": comment}
    if supervisor:
        supervisor["acknowledged"] = weighed is not None and key(weighed["comment"]) >= key(supervisor["comment"])
        return supervisor
    return weighed or reported


def completion_report(record, io, repo, pr, entries, logins, request, evidence):
    receipt = record.get("receipt", {})
    session, author = receipt.get("author_session"), receipt.get("author_provider")
    error = None
    known_session = any(e["session_id"] == session and e.get("repository", "").casefold() == repo.casefold()
                        and e.get("supervisor_owned") is True for e in entries)
    if author not in {"openai", "anthropic"} or not known_session:
        try:
            session, author = bind_session(io, repo, {pr["head"]["ref"]}, entries, logins, request, pr)
        except (ValueError, GitHubReadError, GitHubReadShapeError) as reason:
            session, author, error = None, None, str(reason)
    confirmed = local_completion(evidence, repo, pr["number"], request, pr["head"]["sha"], session, author)
    edited = record.get("request_edited_after_review", False)
    comment = record["comment"]
    status = {"supervisor": "already_completed", "weighed": "already_weighed", "reported": "reported_review"}
    return {"status": status[record["kind"]],
            "completion_kind": record["kind"], "requested_provider_confirmed": confirmed,
            "request_edited_after_review": edited, "needs_attention": not confirmed or edited or error is not None,
            "head": pr["head"]["sha"], "request": request["id"],
            "author_session": session, "author_provider": author, "notification_error": error,
            "notification_needed": record["kind"] != "weighed" and not record.get("acknowledged", False),
            "completion_comment": comment.get("html_url") or comment.get("url") or comment["id"]}


def receipt_for(request, head):
    return {"request": request["id"], "head": head}


def journal_key(repo, number, request, head):
    binding = json.dumps(receipt_for(request, head), sort_keys=True)
    return f"{repo.replace('/', '-')}-{number}-" + hashlib.sha256(binding.encode()).hexdigest()


def local_completion(evidence, repo, number, request, head, session, author):
    """A shared bot comment alone cannot prove that requested inference completed."""
    key = journal_key(repo, number, request, head)
    try:
        state = json.loads((evidence / f"{key}.json").read_text())
        review = state.get("review") or {}
        provider = {"openai": "anthropic", "anthropic": "openai"}.get(author)
        return (state.get("phase") in {"post_pending", "post_attempted", "post_unknown", "posted"}
                and state.get("head") == head and state.get("request") == request["id"]
                and state.get("author_session") == session and session is not None
                and state.get("author_provider") == author and provider is not None
                and review.get("ok") is True and review.get("provider") == provider
                and bool((evidence / f"{key}.md").read_text().strip()))
    except (ValueError, OSError, AttributeError, TypeError):
        return False


def inline_prompt(body):
    # The surrounding request is context, not a substitute for a missing prompt.
    match = re.search(r"(?ims)(?:^|[ \t])(?:Review )?Prompt:\s*\n+```[^\n]*\n(.*?)^```", body)
    if match and match[1].strip():
        return match[1].strip()
    match = re.search(r"(?ims)(?:^|[ \t])(?:Review )?Prompt:\s*\n+(.*)", body)
    if match:
        text = re.sub(r"(?s)<!--.*?-->", "", match[1]).strip()
        if text:
            return text
    quoted = "\n".join(line[1:].lstrip() for line in body.splitlines() if line.startswith(">"))
    if quoted.strip():
        return quoted.strip()
    if (re.search(r"\bread.only\b", body, re.I) and re.search(r"\bRead\s+", body)
            and re.search(r"\bCheck\s+", body)):
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


def bind_head(request, head):
    context = re.split(r"(?im)(?:^|[ \t])(?:Review )?Prompt:|^>|^```", request["body"], maxsplit=1)[0]
    explicit = re.findall(r"(?im)^Head(?: SHA)?:\s*`?([0-9a-f]{7,40})\b", context)
    heads = explicit or HEAD.findall(context)
    if not heads:
        # Legacy prose may say "on SHA"; short numeric dates and counts are not
        # head declarations. Abbreviated legacy hashes require code formatting.
        legacy = re.findall(r"\b(?:on|at)\s+(?:`([0-9a-f]{7,40})`|([0-9a-f]{40})\b)", context, re.I)
        heads = [short or full for short, full in legacy]
    if not heads or any(not head.startswith(value.lower()) for value in heads):
        raise ReviewError("request head missing or moved; renew the request with Head: FULL_SHA")


def bind_session(io, repo, branches, entries, logins, request, pr):
    candidates = {entry["session_id"]: entry for entry in entries
                  if entry.get("repository", "").casefold() == repo.casefold()
                  and entry.get("supervisor_owned") is True}
    owned_issues = {entry.get("issue") for entry in candidates.values()
              if isinstance(entry.get("issue"), int) and not isinstance(entry["issue"], bool) and entry["issue"] > 0}
    # A brief may name a parent before its worker creates the implementation issue.
    # References discover claims only; the trusted claim must still bind branch and session.
    issues = set(owned_issues)
    issues.update(ref["number"] for ref in issue_references(pr.get("body") or "", repo=repo)
                  if ref["repo"].casefold() == repo.casefold())
    matches, outside = {}, set()
    for issue in sorted(issues):
        for comment in io.paged_json(f"/repos/{repo}/issues/{issue}/comments", step_prefix="author_claims"):
            if trusted(comment, logins):
                for line in live_lines(comment.get("body") or ""):
                    try:
                        claims = records(line)
                    except ValueError:
                        # A referenced issue may carry an incomplete ownership record
                        # for another branch. Never ignore a matching or unknown branch.
                        match = re.fullmatch(r"\s*<!-- github-plan:claim (.*?) -->\s*", line)
                        raw = json.loads(match[1]) if match else None
                        if (issue not in owned_issues and isinstance(raw, dict)
                                and isinstance(raw.get("branch"), str) and raw["branch"] and raw["branch"] not in branches):
                            continue
                        raise
                    for claim in claims:
                        if claim["branch"] in branches:
                            if claim["session"] in candidates:
                                matches[claim["session"]] = candidates[claim["session"]]
                            else:
                                outside.add(claim["session"])
    explicit = re.findall(r"(?im)^Author session:\s*`?([^`\s]+)`?\s*$", request["body"])
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


def find_worktree(checkout, repo, branch, head, preferred=None):
    candidates = []
    raw = git(checkout, "worktree", "list", "--porcelain", "-z")
    for block in raw.split("\0\0"):
        fields = dict(line.partition(" ")[::2] for line in block.split("\0") if line)
        if fields.get("HEAD") == head and fields.get("branch"):
            candidates.append((Path(fields["worktree"]), fields["branch"].removeprefix("refs/heads/")))
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

    def get_json(self, path, **kwargs):
        return self.reader.get_json(path, **kwargs)

    def paged_json(self, path, **kwargs):
        if kwargs.get("step_prefix") == "author_claims":
            if path not in self.claims:
                self.claims[path] = self.reader.paged_json(path, **kwargs)
            return self.claims[path]
        return self.reader.paged_json(path, **kwargs)

    def review(self, author, worktree, prompt, output):
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
            raise ReviewError(f"review failed; no result posted: {json.dumps(metadata)}")
        return metadata

    def post(self, repo, number, body):
        result = run(["uv", "run", str(ROOT / "skills/github/scripts/gh-pr.py"), "--repo", repo,
                      "comment", str(number), "--body-file", str(body)], timeout=180)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise PostError("posting returned invalid metadata; do not replay: " + redact_string(result.stderr.strip())) from error
        if (not isinstance(payload, dict) or result.returncode or payload.get("ok") is not True
                or payload.get("outcome_certainty") not in {"confirmed", "reconciled_applied"}):
            certainty = payload.get("outcome_certainty") if isinstance(payload, dict) else None
            phase = "post_failed" if certainty in {"confirmed", "confirmed_not_applied", "reconciled_not_applied"} else "post_unknown"
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


def dispatch(io, repo, number, entries, checkout, logins, evidence, apply=False):
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
    try:
        bind_session(io, repo, {branch}, entries, logins, request, pr)
    except OtherRun as error:
        return {"status": "not_owned", "head": head, "request": request["id"], "reason": str(error)}
    except (ValueError, GitHubReadError, GitHubReadShapeError):
        # Completion can still be reported without a live claim lookup. Pending
        # requests must establish ownership after selecting the exact worktree.
        pass
    existing = completion_record(comments, request, head, logins)
    if existing:
        return completion_report(existing, io, repo, pr, entries, logins, request, evidence)
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
    if not apply:
        return report
    existing = verify_inputs(io, repo, number, pr, worktree, worktree_branch, request, sources, logins)
    if existing:
        return completion_report(existing, io, repo, pr, entries, logins, request, evidence)
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = journal_key(repo, number, request, head)
    journal = evidence / f"{key}.json"
    # Exclusive creation survives process interruption and unknown remote writes.
    # A later sweep reads remote completion first, then refuses a spent launch.
    try:
        with journal.open("x") as stream:
            json.dump({**report, "phase": "launch_started"}, stream, indent=2)
    except FileExistsError as error:
        raise ReviewError(f"prior launch exists at {journal}; inspect its outcome, do not replay a refusal or unknown write") from error
    with tempfile.TemporaryDirectory(prefix=f"{repo.replace('/', '-')}-{number}-", dir=evidence) as scratch:
        folder = Path(scratch)
        prompt_file, output = folder / "prompt.md", folder / "review.md"
        prompt_file.write_text(prompt)
        try:
            metadata = io.review(author, worktree, prompt_file, output)
            # The adapter's declared-completion contract must include actual text.
            review = output.read_text().strip()
            if not review or metadata.get("ok") is not True:
                raise ReviewError("review completion missing or empty")
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            journal.write_text(json.dumps({**report, "phase": "review_failed", "error": str(error)}, indent=2))
            raise
        # Keep every completed review before any freshness check or remote write.
        # Changed inputs prevent posting, but cannot discard paid inference.
        result_file = evidence / f"{key}.md"
        result_file.write_text(review + "\n")
        retained = {**report, "review": metadata, "evidence": str(result_file)}
        journal.write_text(json.dumps({**retained, "phase": "review_completed"}, indent=2))
        existing = verify_inputs(io, repo, number, pr, worktree, worktree_branch, request, sources, logins)
        if existing:
            return completion_report(existing, io, repo, pr, entries, logins, request, evidence)
        journal.write_text(json.dumps({**retained, "phase": "post_pending"}, indent=2))
        body = folder / "post.md"
        body.write_text(f"Review of record, run by the Supervisor\n\nHead: {head}\n"
                        f"Provider: {metadata['provider']}\nModel: {metadata.get('model', 'unknown')}\n"
                        f"Model source: {metadata.get('model_source', 'unknown')}\n"
                        f"Authoring session to notify: {session}\n\n"
                        "The author weighs these findings before ready.\n\n" + review + "\n\n"
                        "<!-- supervisor-review " + json.dumps({**receipt_for(request, head), "author_session": session, "author_provider": author,
                            "request_updated_at": request.get("updated_at") or request["created_at"]}, sort_keys=True) + " -->\n")
        journal.write_text(json.dumps({**retained, "phase": "post_attempted"}, indent=2))
        try:
            receipt = io.post(repo, number, body)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            journal.write_text(json.dumps({**retained, "phase": getattr(error, "phase", "post_unknown"),
                                           "error": str(error), "post": getattr(error, "payload", None)}, indent=2))
            raise
        journal.write_text(json.dumps({**retained, "phase": "posted", "post": receipt}, indent=2))
        return {**report, "status": "posted", "post": receipt, "evidence": str(result_file),
                "notification_needed": True,
                "completion_comment": receipt.get("url") or (receipt.get("comment") or {}).get("url")}


def sweep(io, entries, checkouts, logins, evidence, apply=False):
    reports = []
    repos = sorted({e["repository"].casefold() for e in entries if e.get("supervisor_owned") is True})
    checkouts = {repo.casefold(): path for repo, path in checkouts.items()}
    for repo in repos:
        print(f"Discovering Supervisor reviews in {repo}", file=sys.stderr, flush=True)
        try:
            pulls = io.paged_json(f"/repos/{repo}/pulls", step_prefix="open_prs", params={"state": "open"})
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
    parser.add_argument("--evidence-dir", type=Path, required=True)
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
