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
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "skills/github/scripts"))
from github_api import redact_string
from github_direction_next import issue_references
from github_identity import automation_login, configured_bot_logins
from github_plan_claim import records
from github_read import (GitHubReader, GitHubReadError, GitHubReadShapeError,
                         automation_only_gh_prefix_args, resolve_repo_from_remote)
from ready_handoff import review_receipt_head

REPO = re.compile(r"[\w.-]+/[\w.-]+", re.ASCII)
REQUEST = re.compile(r"supervisor runs the review", re.I)
COMMENT_LINK = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)#issuecomment-(\d+)")
HEAD = re.compile(r"\b(?:head(?: SHA)?|commit)\s*[:=]?\s*`?([0-9a-f]{7,40})\b", re.I)
RECEIPT = re.compile(r"<!-- supervisor-review (.*?) -->")


class ReviewError(ValueError):
    """An unsafe or ambiguous binding; no alternative launch or identity is tried."""


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL, **kwargs)


def review_entries(path):
    entries = json.loads(path.read_text())
    if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
        raise ReviewError("ledger must be an array of session objects")
    selected, seen = [], set()
    for entry in entries:
        if entry.get("supervisor_owned") is not True:
            continue
        if (not isinstance(entry.get("session_id"), str) or not entry["session_id"].strip()
                or entry["session_id"] in seen or entry.get("harness") not in ("codex", "claude")
                or not isinstance(entry.get("repository"), str) or not REPO.fullmatch(entry["repository"])):
            raise ReviewError("Supervisor entry needs a unique session_id, native harness and OWNER/REPO")
        seen.add(entry["session_id"])
        selected.append(entry)
    return selected


def trusted(comment, logins):
    return (comment.get("user") or {}).get("login", "").casefold() in logins


def latest_request(comments, logins):
    asks = [c for c in comments if trusted(c, logins) and REQUEST.search(c.get("body") or "")
            and not RECEIPT.search(c.get("body") or "")]
    return max(asks, key=lambda c: (c.get("updated_at") or c["created_at"], c["id"]), default=None)


def completed(comments, request, head, logins):
    for comment in comments:
        if not trusted(comment, logins):
            continue
        body = comment.get("body") or ""
        if (review_receipt_head(body) == head
                and (comment.get("updated_at") or comment["created_at"]) >=
                (request.get("updated_at") or request["created_at"])):
            return True
        for match in RECEIPT.finditer(body):
            receipt = json.loads(match[1])
            if receipt == receipt_for(request, head):
                return True
    return False


def receipt_for(request, head):
    return {"request": request["id"], "request_updated_at": request.get("updated_at") or request["created_at"],
            "head": head}


def inline_prompt(body):
    # The surrounding request is context, not a substitute for a missing prompt.
    match = re.search(r"(?ims)^Prompt:\s*\n+```[^\n]*\n(.*?)^```", body)
    if match and match[1].strip():
        return match[1].strip()
    quoted = "\n".join(line[1:].lstrip() for line in body.splitlines() if line.startswith(">"))
    if quoted.strip():
        return quoted.strip()
    match = re.search(r"(?ims)^Prompt:\s*\n+(.*)", body)
    if match:
        text = re.sub(r"(?s)<!--.*?-->", "", match[1]).strip()
        if text:
            return text
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
    context = re.split(r"(?im)^Prompt:|^>|^```", request["body"], maxsplit=1)[0]
    heads = HEAD.findall(context)
    if not heads or any(not head.startswith(value.lower()) for value in heads):
        raise ReviewError("request head missing or moved; renew the request with Head: FULL_SHA")


def bind_session(io, repo, branches, entries, logins, request, pr):
    candidates = {entry["session_id"]: entry for entry in entries
                  if entry.get("repository", "").casefold() == repo.casefold()
                  and entry.get("supervisor_owned") is True}
    issues = {entry.get("issue") for entry in candidates.values()
              if isinstance(entry.get("issue"), int) and not isinstance(entry["issue"], bool) and entry["issue"] > 0}
    # A brief may name a parent before its worker creates the implementation issue.
    # References discover claims only; the trusted claim must still bind branch and session.
    issues.update(ref["number"] for ref in issue_references(pr.get("body") or "", repo=repo)
                  if ref["repo"].casefold() == repo.casefold())
    matches = {}
    for issue in sorted(issues):
        for comment in io.paged_json(f"/repos/{repo}/issues/{issue}/comments", step_prefix="author_claims"):
            if trusted(comment, logins):
                for claim in records(comment.get("body") or ""):
                    if claim["branch"] in branches and claim["session"] in candidates:
                        matches[claim["session"]] = candidates[claim["session"]]
    explicit = re.findall(r"(?im)^Author session:\s*`?([^`\s]+)`?\s*$", request["body"])
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
    if git(path, "status", "--porcelain", "--untracked-files=all"):
        raise ReviewError("dirty worktree (including untracked files)")


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
                      "--prompt-file", str(prompt), "--out", str(output)], timeout=1900)
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
        payload = json.loads(result.stdout)
        if (not isinstance(payload, dict) or result.returncode or payload.get("ok") is not True
                or payload.get("outcome_certainty") != "confirmed"):
            raise ReviewError(f"posting not confirmed; do not replay: {json.dumps(payload)}")
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
        if current != source:
            raise ReviewError("review request or linked prompt changed")
    comments = io.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="verify_review_comments")
    if latest_request(comments, logins) != request:
        raise ReviewError("review request superseded")
    return completed(comments, request, head, logins)


def dispatch(io, repo, number, entries, checkout, logins, evidence, apply=False):
    path = f"/repos/{repo}/pulls/{number}"
    pr = io.get_json(path, step="review_pr")
    if pr["state"] != "open":
        return {"status": "closed"}
    if (pr["head"].get("repo") or {}).get("full_name", "").casefold() != repo.casefold():
        raise ReviewError("fork head has no verified local binding")
    comments_path = f"/repos/{repo}/issues/{number}/comments"
    comments = io.paged_json(comments_path, step_prefix="review_comments")
    request = latest_request(comments, logins)
    if request is None:
        return {"status": "no_request"}
    head, branch = pr["head"]["sha"], pr["head"]["ref"]
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ReviewError("invalid PR head")
    if completed(comments, request, head, logins):
        return {"status": "already_completed"}
    bind_head(request, head)
    prompt, sources = resolve_prompt(io, repo, request, logins)
    paths = re.findall(r"(?im)^Worktree:\s*`?([^`\n]+?)`?\s*$", request["body"])
    if len(paths) > 1 or (paths and not Path(paths[0]).is_absolute()):
        raise ReviewError("Worktree: must name one absolute registered path")
    worktree = find_worktree(checkout, repo, branch, head, Path(paths[0]) if paths else None)
    worktree_branch = git(worktree, "symbolic-ref", "--short", "HEAD")
    session, author = bind_session(io, repo, {branch, worktree_branch}, entries, logins, request, pr)
    report = {"status": "pending", "head": head, "request": request["id"], "author_session": session,
              "author_provider": author, "worktree": str(worktree)}
    if not apply:
        return report
    if verify_inputs(io, repo, number, pr, worktree, worktree_branch, request, sources, logins):
        return {**report, "status": "already_completed"}
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    binding = json.dumps(receipt_for(request, head), sort_keys=True)
    key = f"{repo.replace('/', '-')}-{number}-" + hashlib.sha256(binding.encode()).hexdigest()
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
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            journal.write_text(json.dumps({**report, "phase": "review_failed", "error": str(error)}, indent=2))
            raise
        # Review helper exit 0 with its output file is its declared-completion contract.
        # Check again here so a failed adapter cannot publish an empty report.
        review = output.read_text().strip()
        if not review or metadata.get("ok") is not True:
            raise ReviewError("review completion missing or empty")
        if verify_inputs(io, repo, number, pr, worktree, worktree_branch, request, sources, logins):
            return {**report, "status": "already_completed"}
        # Keep durable evidence before a possibly uncertain comment write. Never retry here.
        journal.write_text(json.dumps({**report, "phase": "post_pending", "review": metadata}, indent=2))
        result_file = evidence / f"{key}.md"
        result_file.write_text(review + "\n")
        body = folder / "post.md"
        body.write_text(f"Review of record, run by the Supervisor\n\nHead: {head}\n"
                        f"Provider: {metadata['provider']}\nModel: {metadata.get('model', 'unknown')}\n"
                        f"Model source: {metadata.get('model_source', 'unknown')}\n"
                        f"Authoring session to notify: {session}\n\n"
                        "The author weighs these findings before ready.\n\n" + review + "\n\n"
                        "<!-- supervisor-review " + json.dumps(receipt_for(request, head), sort_keys=True) + " -->\n")
        receipt = io.post(repo, number, body)
        journal.write_text(json.dumps({**report, "phase": "posted", "post": receipt}, indent=2))
        return {**report, "status": "posted", "post": receipt, "evidence": str(result_file)}


def sweep(io, entries, checkouts, logins, evidence, apply=False):
    reports = []
    repos = sorted({e["repository"] for e in entries if e.get("supervisor_owned") is True})
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
                if repo not in checkouts:
                    raise ReviewError("repository checkout missing; pass --checkout OWNER/REPO=/absolute/path")
                result = dispatch(io, repo, number, entries, checkouts[repo], logins, evidence, apply)
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
        entries = review_entries(args.ledger)
        checkouts = {}
        for mapping in args.checkout:
            repo, sep, path = mapping.partition("=")
            if not sep or not REPO.fullmatch(repo) or not Path(path).is_absolute() or repo in checkouts:
                raise ReviewError("each --checkout needs a unique OWNER/REPO=/absolute/path")
            checkouts[repo] = Path(path)
        logins = {v.casefold() for v in (*configured_bot_logins(), automation_login()) if v}
        if not logins:
            raise ReviewError("automation identities are not configured")
        # Shared evidence location serializes invocations; a concurrent pass never duplicates inference/posts.
        args.evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (args.evidence_dir / "pending-reviews.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            reports = sweep(IO(), entries, checkouts, logins, args.evidence_dir, args.apply)
        print(json.dumps({"results": reports}, indent=2))
        return int(any(r["status"] == "refused" for r in reports))
    except (ValueError, OSError) as error:
        print(json.dumps({"error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
