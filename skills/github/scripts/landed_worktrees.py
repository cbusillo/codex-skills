#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlencode

import dev_worktree
import github_plan_claim
import github_plan_release


# The host helper's lock reason ends with "retire with: dev-worktree retire <path>".
RETIRE_MARKER = "retire with: dev-worktree retire"
DEV_WORKTREE = Path(__file__).with_name("dev-worktree")
ANY_CLAIM = {"worker": "", "session": "", "branch": "", "claimed_at": ""}
NO_WORKTREES = {"worktrees": []}
FRESH_START = dt.timedelta(hours=2)
SEARCH_PAGE = 100


@dataclass(frozen=True)
class Worktree:
    path: Path
    head: str
    branch: str | None
    lock: str | None

    @property
    def names_retire_command(self) -> bool:
        return self.lock is not None and RETIRE_MARKER in self.lock


def git(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True,
                          env=dev_worktree.git_environment())


def registered(checkout: Path) -> list[Worktree]:
    """Linked worktrees of the checkout's repository; the main checkout is never listed."""
    result = git(checkout, "worktree", "list", "--porcelain", "-z")
    if result.returncode:
        raise ValueError(result.stderr.strip() or "git worktree list failed")
    worktrees: list[Worktree] = []
    for record in result.stdout.split("\0\0"):
        fields: dict[str, str] = {}
        for line in filter(None, record.split("\0")):
            key, _, value = line.partition(" ")
            fields[key] = value
        if "worktree" not in fields or "bare" in fields:
            continue
        branch = fields.get("branch")
        worktrees.append(Worktree(
            path=Path(fields["worktree"]), head=fields.get("HEAD", ""),
            branch=branch.removeprefix("refs/heads/") if branch else None,
            lock=fields.get("locked") if "locked" in fields else None,
        ))
    return worktrees[1:]


def contained(checkout: Path, head: str, tip: str) -> bool:
    """True when every commit at head is in tip; a tip missing locally proves nothing."""
    if head == tip:
        return True
    return (git(checkout, "cat-file", "-e", f"{tip}^{{commit}}").returncode == 0
            and git(checkout, "merge-base", "--is-ancestor", head, tip).returncode == 0)


def is_under(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def first_parent_history(checkout: Path) -> set[str]:
    tip = default_tracking_tip(checkout)
    return set(git(checkout, "rev-list", "--first-parent", tip).stdout.split()) if tip else set()


def landed_decisions(checkout: Path, branch: str, pr_head: str, root: Path, *,
                     pr_commits: set[str] = frozenset(), claims: str = "") -> list[dict]:
    fresh_starts = first_parent_history(checkout) if pr_commits else set()
    rows = []
    for tree in registered(checkout):
        fed_the_pr = tree.branch != branch and tree.head in pr_commits
        if tree.branch != branch and not fed_the_pr:
            continue
        row = {"path": str(tree.path), "branch": tree.branch, "head": tree.head}
        if not is_under(tree.path, root):
            row.update(decision="kept", reason="outside the configured worktree root")
        elif not tree.names_retire_command:
            row.update(decision="kept", reason="lock names no retire command; follow work-closeout")
        elif tree.branch and tree.branch in claims:
            row.update(decision="kept", reason="an open claim names this branch")
        elif fed_the_pr and (tree.head in fresh_starts or not fresh_starts):
            row.update(decision="kept", reason="HEAD may be a fresh start on the default branch")
        elif not fed_the_pr and not contained(checkout, tree.head, pr_head):
            row.update(decision="kept", reason="local commits beyond the landed head")
        else:
            row.update(decision="candidate")
        rows.append(row)
    return rows


def run_retire(path: str, *, dry_run: bool) -> subprocess.CompletedProcess[str]:
    args = [str(DEV_WORKTREE), "retire", *(["--dry-run"] if dry_run else []), path]
    return subprocess.run(args, capture_output=True, text=True)


def retire(rows: list[dict], *, apply: bool, runner=None) -> list[dict]:
    """Preview every candidate with the host helper, then retire it when applying."""
    runner = runner or run_retire
    for row in rows:
        if row["decision"] != "candidate":
            continue
        preview = runner(row["path"], dry_run=True)
        if preview.returncode:
            row.update(decision="kept", reason="retire preview refused",
                       detail=preview.stderr.strip().splitlines()[-1:] or [""])
            continue
        if not apply:
            row["decision"] = "would_retire"
            continue
        result = runner(row["path"], dry_run=False)
        if result.returncode:
            row.update(decision="kept", reason="retire refused",
                       detail=result.stderr.strip().splitlines()[-1:] or [""])
        else:
            row["decision"] = "retired"
    return rows


def finished_pull(pull: dict) -> bool:
    return pull.get("state") == "closed"


def github_slug(checkout: Path) -> str | None:
    url = git(checkout, "remote", "get-url", "origin").stdout.strip()
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if url.startswith(prefix):
            slug = url.removeprefix(prefix).removesuffix(".git")
            return slug if slug.count("/") == 1 else None
    return None


def default_tracking_tip(checkout: Path) -> str | None:
    ref = git(checkout, "symbolic-ref", "-q", "refs/remotes/origin/HEAD").stdout.strip()
    result = git(checkout, "rev-parse", "--verify", "-q", f"{ref or 'refs/remotes/origin/main'}^{{commit}}")
    return result.stdout.strip() if result.returncode == 0 else None


def finished(root: Path, pulls_for, include=lambda slug: True) -> tuple[list[dict], list[dict]]:
    """Worktrees under root that the landing step should have retired, and unread scopes.

    pulls_for(slug, branch) returns that branch's PRs, or raises LookupError when
    they cannot be read. A branch already in the default branch's tracking ref
    needs no GitHub read. include(slug) limits the scan to the caller's scope.
    """
    found: list[dict] = []
    gaps: list[dict] = []
    seen: set[Path] = set()
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink()):
        commons = {}
        for task in sorted(p for p in folder.iterdir() if p.is_dir() and not p.is_symlink()):
            common = git(task, "rev-parse", "--path-format=absolute", "--git-common-dir")
            if common.returncode == 0:
                commons.setdefault(Path(common.stdout.strip()), task)
        for common, task in commons.items():
            if common in seen:
                continue
            seen.add(common)
            slug = github_slug(task)
            if slug is None or not include(slug):
                continue
            tip = default_tracking_tip(task)
            try:
                trees = registered(task)
            except ValueError:
                gaps.append({"source": folder.name, "reason": "worktree_list_unreadable"})
                continue
            for tree in trees:
                if not tree.names_retire_command or not tree.branch or not is_under(tree.path, folder):
                    continue
                row = {"repo": slug, "task": tree.path.name, "branch": tree.branch,
                       "dirty": bool(git(tree.path, "status", "--porcelain").stdout.strip()), "pull": None}
                if tip and contained(task, tree.head, tip):
                    # No commits of its own: date it from creation or last checkout, so a
                    # session that has only just started is inside the age threshold.
                    since = last_checkout(tree.path)
                    found.append({**row, "basis": "in default branch", "since": since.isoformat() if since else None})
                    continue
                try:
                    pulls = pulls_for(slug, tree.branch)
                except LookupError as exc:
                    gaps.append({"source": f"{slug}:{tree.branch}", "reason": str(exc) or "pull_read_unavailable"})
                    continue
                if not pulls or any(p.get("state") != "closed" for p in pulls):
                    continue
                covering = [p for p in pulls if contained(task, tree.head, (p.get("head") or {}).get("sha", ""))]
                if covering:
                    pull = max(covering, key=lambda p: p.get("closed_at") or "")
                    found.append({**row, "basis": "PR merged" if pull.get("merged_at") else "PR closed",
                                  "since": pull.get("closed_at"),
                                  "pull": {"url": pull.get("html_url"), "number": pull.get("number")}})
    return found, gaps


def last_checkout(path: Path) -> dt.datetime | None:
    admin = git(path, "rev-parse", "--absolute-git-dir").stdout.strip()
    try:
        return dt.datetime.fromtimestamp(Path(admin, "HEAD").stat().st_mtime, dt.timezone.utc)
    except OSError:
        return None


def keep_fresh_starts(rows: list[dict], now: dt.datetime) -> list[dict]:
    for row in rows:
        since = last_checkout(Path(row["path"]))
        if row["decision"] == "candidate" and (since is None or now - since < FRESH_START):
            row.update(decision="kept", reason="checked out within the last two hours; may be a fresh start")
    return rows


def unless_fast_forwarded(pull: dict, pr_commits: set[str]) -> set[str]:
    return set() if pull.get("merge_commit_sha") in pr_commits else pr_commits


def open_claims(reader, github_read, repo: str, number: int, pull: dict) -> str:
    import github_plan_landing
    import github_plan_status.references

    records = [(repo, number), *github_plan_status.references.linked_issues(repo, pull)]
    base = pull.get("base") or {}
    if pull.get("merged_at") and base.get("ref") == (base.get("repo") or {}).get("default_branch"):
        records += github_plan_landing.closing_references(reader, repo, number)[1]
    return claims_on(reader, github_read, records)


def issues_naming(reader, owner: str, branch: str) -> list[tuple[str, int]]:
    query = f'"{branch}" user:{owner} is:issue'
    result = reader.request("GET", "search/issues?" + urlencode({"q": query, "per_page": SEARCH_PAGE}),
                            step="landed_branch_claims", bucket="search").body
    items = result.get("items") if isinstance(result, dict) else None
    if (not isinstance(items, list) or result.get("incomplete_results") is not False
            or result.get("total_count") != len(items)):
        raise ValueError(f"issue search for {branch} is incomplete")
    return [(item["repository_url"].split("/repos/", 1)[1], item["number"]) for item in items]


def claims_on(reader, github_read, records: list[tuple[str, int]]) -> str:
    def release_evidence(evidence_repo, _issue, comment_id):
        try:
            return reader.get_json(f"repos/{evidence_repo}/issues/comments/{comment_id}", step="landed_release_evidence")
        except github_read.GitHubReadError as exc:
            if getattr(exc.result, "status", None) in {403, 404}:
                return None
            raise

    found = []
    for record_repo, record_number in dict.fromkeys(records):
        issue = reader.get_json(f"repos/{record_repo}/issues/{record_number}", step="landed_claim_record")
        comments = reader.paged_json(f"repos/{record_repo}/issues/{record_number}/comments",
                                     step_prefix="landed_claim_discussion")
        comments = github_plan_release.verified_comments(comments, release_evidence)
        released = {source["claim_id"] for source in github_plan_claim.ended_ownership(comments, NO_WORKTREES)}
        conflicts, _ = github_plan_claim.discussion_evidence((issue or {}).get("body") or "", comments, ANY_CLAIM,
                                                             ended_claims=released)
        found += [json.dumps(c["record"]) if "record" in c else c.get("text", "") for c in conflicts]
    return "\n".join(found)


def same_repository(checkout_slug: str | None, repo: str) -> bool:
    return bool(checkout_slug) and checkout_slug.casefold() == repo.casefold()


def refuse(message: str) -> int:
    print(json.dumps({"ok": False, "error": message}))
    return 2


def pull_rows(reader, github_read, repo: str, number: int, checkout: Path, root: Path) -> tuple[dict, list[dict]]:
    try:
        pull = reader.get_json(f"repos/{repo}/pulls/{number}", step="landed_pull")
    except (github_read.GitHubReadError, github_read.GitHubReadShapeError) as exc:
        raise LookupError(f"PR read unavailable: {exc}") from exc
    if not isinstance(pull, dict) or not finished_pull(pull):
        raise LookupError("PR is not merged or closed; nothing is retired")
    head = pull.get("head") or {}
    if ((head.get("repo") or {}).get("full_name") or "").casefold() != repo.casefold():
        return {"note": "PR head is in another repository"}, []
    try:
        pr_commits = {c.get("sha") for c in reader.paged_json(f"repos/{repo}/pulls/{number}/commits",
                                                              step_prefix="landed_pull_commits")}
        pr_commits = unless_fast_forwarded(pull, pr_commits)
        claims = open_claims(reader, github_read, repo, number, pull)
    except (github_read.GitHubReadError, github_read.GitHubReadShapeError, ValueError, TypeError) as exc:
        raise LookupError(f"PR commits or claims unavailable; nothing is retired: {exc}") from exc
    rows = landed_decisions(checkout, head["ref"], head["sha"], root, pr_commits=pr_commits, claims=claims)
    return {"merged": bool(pull.get("merged_at"))}, rows


def branch_rows(reader, github_read, repo: str, branch: str, checkout: Path, root: Path) -> tuple[dict, list[dict]]:
    try:
        pulls = reader.paged_json(f"repos/{repo}/pulls", step_prefix="landed_branch_pulls",
                                  params={"state": "all", "head": f"{repo.split('/')[0]}:{branch}"})
    except (github_read.GitHubReadError, github_read.GitHubReadShapeError) as exc:
        raise LookupError(f"PR read for {branch} unavailable: {exc}") from exc
    if pulls:
        raise LookupError(f"{branch} has PR #{pulls[0].get('number')}; retire with --pr instead")
    tip = default_tracking_tip(checkout)
    if not tip:
        raise LookupError("the default branch's tracking ref is missing; fetch origin first")
    try:
        claims = claims_on(reader, github_read, issues_naming(reader, repo.split("/")[0], branch))
    except (github_read.GitHubReadError, github_read.GitHubReadShapeError, ValueError, TypeError, KeyError) as exc:
        raise LookupError(f"claims naming {branch} unavailable; nothing is retired: {exc}") from exc
    rows = landed_decisions(checkout, branch, tip, root, claims=claims)
    return {}, keep_fresh_starts(rows, dt.datetime.now(dt.timezone.utc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Retire a landed PR's worktrees with the command each lock names.")
    parser.add_argument("--repo", required=True, help="OWNER/REPO of the landed work")
    landed = parser.add_mutually_exclusive_group(required=True)
    landed.add_argument("--pr", type=int)
    landed.add_argument("--branch", help="a task branch that never had a PR and is in the default branch")
    parser.add_argument("--checkout", required=True, type=Path, help="any local checkout of that repository")
    parser.add_argument("--apply", action="store_true", help="retire; the default only previews")
    args = parser.parse_args(argv)
    try:
        root, helper = dev_worktree.configuration()
    except (dev_worktree.Refused, OSError, json.JSONDecodeError) as exc:
        return refuse(f"worktree configuration: {exc}")
    if helper is None:
        # Portable worktrees carry no retire command; work-closeout removes them.
        print(json.dumps({"ok": True, "worktrees": [], "note": "no hostHelper configured; nothing to retire"}))
        return 0
    checkout_slug = github_slug(args.checkout)
    if not same_repository(checkout_slug, args.repo):
        return refuse(f"checkout origin is {checkout_slug or 'not GitHub'}, not {args.repo}")
    import github_read

    reader = github_read.GitHubReader(operation="github.worktree.retire_landed")
    try:
        if args.branch:
            summary, rows = branch_rows(reader, github_read, args.repo, args.branch, args.checkout, root)
        else:
            summary, rows = pull_rows(reader, github_read, args.repo, args.pr, args.checkout, root)
    except (LookupError, ValueError, OSError) as exc:
        return refuse(str(exc))
    rows = retire(rows, apply=args.apply)
    print(json.dumps({"ok": True, **summary, "worktrees": rows}, indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main())
