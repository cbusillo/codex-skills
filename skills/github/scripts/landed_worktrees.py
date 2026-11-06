#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import importlib.util
import json
import math
import os
import random
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import dev_worktree
import github_plan_claim
import github_plan_release
import published_history

ANY_CLAIM = {"worker": "", "session": "", "branch": "", "claimed_at": ""}
NO_WORKTREES = {"worktrees": []}
FRESH_START = dt.timedelta(hours=2)
SEARCH_PAGE = 100
PREVIEW_TIMEOUT = 30


@dataclass(frozen=True)
class Worktree:
    path: Path
    head: str
    branch: str | None
    lock: str | None


def git(checkout: Path, *args: str, input_text: str | None = None, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        text=True,
        env=dev_worktree.git_environment(),
        input=input_text,
        timeout=timeout,
    )


def registered(checkout: Path, *, include_primary: bool = False, timeout: float = 30) -> list[Worktree]:
    result = git(checkout, "worktree", "list", "--porcelain", "-z", timeout=timeout)
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
    return worktrees if include_primary else worktrees[1:]


def contained(checkout: Path, head: str, tip: str) -> bool:
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
    _, helper = dev_worktree.configuration()
    rows = []
    for tree in registered(checkout):
        fed_the_pr = tree.branch != branch and tree.head in pr_commits
        if tree.branch != branch and not fed_the_pr:
            continue
        row = {"path": str(tree.path), "branch": tree.branch, "head": tree.head}
        if any(is_under(path, tree.path) for path in (checkout, Path.cwd(), Path(__file__))):
            row.update(decision="kept", reason="source checkout or calling session uses this tree")
        elif not is_under(tree.path, root):
            row.update(decision="kept", reason="outside the configured worktree root")
        elif helper is None and tree.lock is not None:
            row.update(decision="kept", reason="portable worktree has a lock; resolve its owning job first")
        elif claims:
            row.update(decision="kept", reason="associated work has an open claim")
        elif fed_the_pr and (tree.head in fresh_starts or not fresh_starts):
            row.update(decision="kept", reason="HEAD may be a fresh start on the default branch")
        elif not fed_the_pr and not contained(checkout, tree.head, pr_head):
            row.update(decision="kept", reason="local commits beyond the landed head")
        else:
            row.update(decision="candidate")
        rows.append(row)
    return rows


def retirement_command(path: str, dry_run: bool) -> subprocess.CompletedProcess[str]:
    _, helper = dev_worktree.configuration()
    if helper is None:
        return portable_retire(Path(path), dry_run=dry_run)
    args = [str(helper), "retire", *(["--dry-run"] if dry_run else []), path]
    environment = dev_worktree.host_environment(args[1:])
    return subprocess.run(args, capture_output=True, text=True, env=environment)


def run_retire(path: str, *, dry_run: bool) -> subprocess.CompletedProcess[str]:
    if not dry_run:
        return retirement_command(path, False)
    source = Path(__file__).parents[2] / "work-closeout/scripts/cleanup_probe.py"
    spec = importlib.util.spec_from_file_location("worktree_preview_probe", source)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load the catalog's bounded preview helper")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    outcome = probe.bounded(retirement_command, (path, True), PREVIEW_TIMEOUT)
    if "result" in outcome:
        return outcome["result"]
    return subprocess.CompletedProcess([], 1, "", f"retire preview {outcome.get('error', 'unavailable')}")


class UnpushedCommit(LookupError):
    pass


def commit_pulls(reader: Any, github_read: Any, repo: str, head: str) -> list[dict]:
    try:
        return reader.paged_json(f"repos/{repo}/commits/{head}/pulls", step_prefix="retirement_commit_pulls")
    except github_read.GitHubReadError as exc:
        if getattr(exc.result, "status", None) == 422 and f"No commit found for SHA: {head}" in str(exc):
            raise UnpushedCommit("unpushed commit; GitHub has no record of this HEAD") from exc
        raise


def delete_merged_branch(checkout: Path, branch: str | None, head: str) -> str:
    if not branch or not branch.startswith("work/"):
        return "local branch kept: not a task branch"
    tip = default_tracking_tip(checkout)
    if not tip or not contained(checkout, head, tip):
        return "local branch kept: not merged into the default branch"
    if any(tree.branch == branch for tree in registered(checkout, include_primary=True)):
        return "local branch kept: still checked out"
    result = git(checkout, "update-ref", "--no-deref", "-d", f"refs/heads/{branch}", head)
    return f"deleted merged local branch {branch}" if result.returncode == 0 else "local branch kept: tip changed or deletion failed"


def publication_git(path: Path | str, *args: str, input_text: str | None = None) -> str:
    result = git(Path(path), "--no-replace-objects", *args, input_text=input_text)
    if result.returncode:
        raise published_history.PublicationError("publication evidence unavailable")
    return result.stdout.strip()


def portable_retire(path: Path, *, dry_run: bool) -> subprocess.CompletedProcess[str]:
    def kept(reason: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, "", f"{reason}; follow work-closeout before retrying")

    status = git(path, "status", "--porcelain", "--ignored", "--untracked-files=all")
    if status.returncode or status.stdout.strip():
        return kept("uncommitted, untracked or ignored files remain")
    try:
        head = git(path, "rev-parse", "HEAD").stdout.strip()
        branch = git(path, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() or None
        published_history.check_retirement(path, publication_git)
    except (published_history.PublicationError, OSError, subprocess.TimeoutExpired) as exc:
        return kept(str(exc))
    try:
        if not has_no_live_users(path, directory=True):
            return kept("worktree is in use or live-use proof is incomplete")
    except (OSError, subprocess.TimeoutExpired):
        return kept("live-use proof unavailable")
    metadata = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    common = Path(metadata.stdout.strip())
    if metadata.returncode or not common.is_absolute() or common.name != ".git":
        return kept("repository identity unavailable")
    inspector = Path(__file__).parents[2] / "jetbrains-inspection/scripts/jb-inspect.py"
    result = subprocess.run(
        [
            sys.executable,
            str(inspector),
            "remove-worktree",
            "--offline-only",
            "--repo",
            str(path),
            *([] if dry_run else ["--no-dry-run"]),
        ],
        capture_output=True,
        text=True,
        env=dev_worktree.git_environment(),
    )
    if result.returncode == 0 and not dry_run:
        if path.exists() or any(tree.path == path for tree in registered(common.parent)):
            return kept("worktree removal postcondition is incomplete")
        result.stdout += "\n" + delete_merged_branch(common.parent, branch, head) + "\n"
    return result


def retire(rows: Iterable[dict], *, apply: bool, runner=None, deadline: float | None = None) -> list[dict]:
    runner = runner or run_retire
    results = []
    for row in rows:
        results.append(row)
        if row["decision"] == "candidate" and deadline is not None and time.monotonic() >= deadline:
            row.update(decision="kept", reason="creation cleanup time budget exhausted; checked on the next start")
        if row["decision"] != "candidate":
            continue
        stage = "preview"
        try:
            preview = runner(row["path"], dry_run=True)
            if preview.returncode:
                row.update(decision="kept", reason="retire preview refused",
                           detail=preview.stderr.strip().splitlines()[-1:] or [""])
                continue
            if not apply:
                row["decision"] = "would_retire"
                continue
            if deadline is not None and time.monotonic() >= deadline:
                row.update(decision="kept", reason="creation cleanup time budget exhausted; checked on the next start")
                continue
            stage = "apply"
            result = runner(row["path"], dry_run=False)
            if result.returncode:
                row.update(decision="kept", reason="retire refused",
                           detail=result.stderr.strip().splitlines()[-1:] or [""])
            else:
                row["decision"] = "retired"
        except Exception as exc:
            row.update(decision="kept" if stage == "preview" else "unknown",
                       reason=f"retire {stage} outcome unavailable; reconcile this exact tree before retrying",
                       detail=[f"{type(exc).__name__}: {exc}"])
    return results


STRAY_RESULT_PATTERNS = (
    "*-inspection*.json",
    "*-inspection*.err",
    "*-checks*.json",
    "*-snapshot*.json",
    "*-watch*.json",
    "*-watch*.jsonl",
    "*-review*.json",
    "*-validation*.log",
    "*-controller.log",
    "*-attention*.json",
    "inspection.json",
    "inspection.err",
    "review.json",
    "error.log",
)


def entry_state(info: os.stat_result) -> dict[str, int]:
    return {field: getattr(info, field) for field in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")}


def stray_snapshot(directory: int) -> dict[str, dict[str, int]]:
    contents = {name: os.stat(name, dir_fd=directory, follow_symlinks=False) for name in os.listdir(directory)}
    if any(
        not stat.S_ISREG(info.st_mode) or not any(fnmatch.fnmatchcase(name, pattern) for pattern in STRAY_RESULT_PATTERNS)
        for name, info in contents.items()
    ):
        raise ValueError("unknown content; retained without deletion")
    return {name: entry_state(info) for name, info in contents.items()}


def archive_results(repository: str, task: str, source_fd: int, contents: dict[str, dict[str, int]]) -> Path:
    evidence, _ = dev_worktree.artifact_roots()
    parent = evidence / repository / task
    if any(path.is_symlink() for path in (parent, *parent.parents)):
        raise ValueError("result evidence directory is redirected")
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if parent.stat().st_dev != os.fstat(source_fd).st_dev:
        raise ValueError("result evidence is on another filesystem; source entries kept")
    destination = Path(tempfile.mkdtemp(prefix="results-", dir=parent))
    target_fd = dev_worktree.open_directory(destination)
    try:
        for name, expected in contents.items():
            if entry_state(os.stat(name, dir_fd=source_fd, follow_symlinks=False)) != expected:
                raise ValueError(f"result changed; remaining files kept; preserved results: {destination}")
            os.rename(name, name, src_dir_fd=source_fd, dst_dir_fd=target_fd)
    except OSError as exc:
        raise OSError(f"result archive incomplete; any moved files are at {destination}: {exc}") from exc
    finally:
        os.close(target_fd)
    return destination


def verify_host_root(root: Path, deadline: float | None) -> None:
    _, helper = dev_worktree.configuration()
    if helper is None:
        return
    verified = subprocess.run(
        [str(helper), "verify-root"],
        capture_output=True,
        text=True,
        timeout=remaining_timeout(deadline, 30),
        env=dev_worktree.host_environment(["verify-root"]),
    )
    if verified.returncode or verified.stdout.strip() != str(root):
        raise ValueError("host worktree root verification unavailable; stray entries retained")


def remaining_timeout(deadline: float | None, maximum: float) -> float:
    remaining = maximum if deadline is None else min(maximum, deadline - time.monotonic())
    if remaining <= 0:
        raise ValueError("creation cleanup time budget exhausted")
    return remaining


def has_no_live_users(path: Path, *, directory: bool, deadline: float | None = None) -> bool:
    users = subprocess.run(
        ["lsof", "-nP", "-t", *(["+D"] if directory else []), str(path)],
        capture_output=True,
        text=True,
        timeout=remaining_timeout(deadline, 10),
    )
    return users.returncode in {0, 1} and not users.stdout.strip() and not users.stderr.strip()


def sweep_strays(checkout: Path, root: Path, *, apply: bool, deadline: float | None = None) -> list[dict]:
    rows = []
    parent = root
    parent_fd = None
    try:
        remaining_timeout(deadline, 30)
        common = Path(dev_worktree.git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        if not common.is_absolute() or common.name != ".git":
            raise ValueError("normal repository identity unavailable; stray entries retained")
        parent = root / common.parent.name
        if not parent.exists():
            return rows
        dev_worktree.artifact_roots()
        dev_worktree.verify_outside_repository(parent)
        if not all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW")):
            raise ValueError("safe directory descriptors unavailable; stray entries retained")
        verify_host_root(root, deadline)
        parent_fd = dev_worktree.open_directory(parent)
        names = os.listdir(parent_fd)
        registered_paths = {
            tree.path.absolute() for tree in registered(checkout, include_primary=True, timeout=remaining_timeout(deadline, 30))
        }
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if parent_fd is not None:
            os.close(parent_fd)
        return [{"path": str(parent), "decision": "kept", "reason": str(exc)}]
    try:
        for index, name in enumerate(names):
            if deadline is not None and time.monotonic() >= deadline:
                rows.append(
                    {
                        "path": str(parent),
                        "decision": "kept",
                        "reason": "creation cleanup time budget exhausted",
                        "remaining_entries": len(names) - index,
                    }
                )
                break
            path = parent / name
            if path.absolute() in registered_paths:
                continue
            row: dict[str, Any] = {"path": str(path), "decision": "kept"}
            rows.append(row)
            directory_fd = None
            try:
                if deadline is not None and time.monotonic() >= deadline:
                    raise ValueError("creation cleanup time budget exhausted")
                info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    directory_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                    contents = stray_snapshot(directory_fd)
                    newest = max([info.st_mtime, *(item["st_mtime_ns"] / 1_000_000_000 for item in contents.values())])
                elif stat.S_ISREG(info.st_mode) and any(fnmatch.fnmatchcase(name, pattern) for pattern in STRAY_RESULT_PATTERNS):
                    contents = {name: entry_state(info)}
                    newest = info.st_mtime
                else:
                    raise ValueError("unknown content or symlink; retained without deletion")
                if time.time() - newest < FRESH_START.total_seconds():
                    raise ValueError("changed within the last two hours; may be in use")
                if is_under(Path.cwd(), path) or is_under(Path(__file__), path):
                    raise ValueError("calling session uses this entry")
                if not has_no_live_users(path, directory=directory_fd is not None, deadline=deadline):
                    raise ValueError("entry is in use or live-use proof is incomplete")
                if (
                    entry_state(os.stat(parent)) != entry_state(os.fstat(parent_fd))
                    or entry_state(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) != entry_state(info)
                    or any(
                        tree.path.resolve() == path.resolve()
                        for tree in registered(checkout, include_primary=True, timeout=remaining_timeout(deadline, 30))
                    )
                    or directory_fd is not None
                    and stray_snapshot(directory_fd) != contents
                ):
                    raise ValueError("entry changed during qualification; retained")
                dev_worktree.verify_outside_repository(parent)
                if apply:
                    if contents:
                        row["evidence"] = str(
                            archive_results(
                                common.parent.name,
                                name if directory_fd is not None else "stray",
                                directory_fd if directory_fd is not None else parent_fd,
                                contents,
                            )
                        )
                    if directory_fd is not None:
                        os.rmdir(name, dir_fd=parent_fd)
                row.update(decision="removed" if apply else "would_remove", files=len(contents))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                row["reason"] = str(exc)
            finally:
                if directory_fd is not None:
                    os.close(directory_fd)
    finally:
        os.close(parent_fd)
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
    try:
        _, helper = dev_worktree.configuration()
    except (ValueError, OSError):
        return [], [{"source": "worktree_configuration", "reason": "unavailable"}]
    found: list[dict] = []
    gaps: list[dict] = []
    seen: set[Path] = set()
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink()):
        commons = {}
        for task in sorted(p for p in folder.iterdir() if p.is_dir() and not p.is_symlink()):
            try:
                common = git(task, "rev-parse", "--path-format=absolute", "--git-common-dir")
            except subprocess.TimeoutExpired:
                gaps.append({"source": f"{folder.name}/{task.name}", "reason": "git_probe_timed_out"})
                continue
            if common.returncode == 0:
                commons.setdefault(Path(common.stdout.strip()), task)
        for common, task in commons.items():
            if common in seen:
                continue
            seen.add(common)
            try:
                slug = github_slug(task)
                if slug is None or not include(slug):
                    continue
                tip = default_tracking_tip(task)
                trees = registered(task)
            except ValueError:
                gaps.append({"source": folder.name, "reason": "worktree_list_unreadable"})
                continue
            except subprocess.TimeoutExpired:
                gaps.append({"source": folder.name, "reason": "git_metadata_timed_out"})
                continue
            for tree in trees:
                try:
                    if (helper is None and tree.lock is not None) or not tree.branch or not is_under(tree.path, folder):
                        continue
                    row = {"repo": slug, "task": tree.path.name, "branch": tree.branch,
                           "dirty": bool(git(tree.path, "status", "--porcelain").stdout.strip()), "pull": None}
                    if tip and contained(task, tree.head, tip):
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
                except subprocess.TimeoutExpired:
                    gaps.append({"source": f"{slug}:{tree.branch}", "reason": "git_read_timed_out"})
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


def pull_owns_tree(pull: dict, tree: Worktree | None) -> bool:
    head = pull.get("head") or {}
    return tree is None or not head or head.get("ref") == tree.branch or head.get("sha") == tree.head


def ownership_decision(reader, github_read, repo: str, pulls: Iterable[dict], branches: Iterable[str], *,
                       tree: Worktree | None = None) -> dict:
    import github_plan_landing
    import github_plan_status.references

    records = []
    names = dict.fromkeys(branches)
    owners = {repo.split("/")[0]: None}
    for pull in pulls:
        if not finished_pull(pull) and pull_owns_tree(pull, tree):
            return {"decision": "kept", "reason": "branch or commit has an open PR"}
        number = pull["number"]
        base = pull.get("base") or {}
        pull_repo = (base.get("repo") or {}).get("full_name") or repo
        owners[pull_repo.split("/")[0]] = None
        records += [(pull_repo, number), *github_plan_status.references.linked_issues(pull_repo, pull)]
        head_ref = (pull.get("head") or {}).get("ref")
        if isinstance(head_ref, str):
            names[head_ref] = None
        if pull.get("merged_at") and base.get("ref") == (base.get("repo") or {}).get("default_branch"):
            records += github_plan_landing.closing_references(reader, pull_repo, number)[1]
    for branch in names:
        if branch:
            for owner in owners:
                records += issues_naming(reader, owner, branch)
    if claims_on(reader, github_read, records, {name for name in names if name}):
        return {"decision": "kept", "reason": "associated work has an open claim"}
    return {"decision": "candidate"}


def target_decision(target: Path, checkout: Path, *, preserve: Iterable[Path] = (), reader: Any = None) -> dict:
    row = {"path": str(target), "decision": "kept"}
    try:
        root, helper = dev_worktree.configuration()
        tree = next((tree for tree in registered(checkout) if tree.path.resolve() == target.resolve()), None)
        if tree is None:
            return {**row, "reason": "target is not a registered linked worktree"}
        row.update(path=str(tree.path), branch=tree.branch, head=tree.head)
        protected = {checkout.resolve(), Path.cwd().resolve(), Path(__file__).resolve(),
                     *(path.resolve() for path in preserve)}
        if any(is_under(path, tree.path) for path in protected):
            return {**row, "reason": "source checkout or calling session uses this tree"}
        if not is_under(tree.path, root):
            return {**row, "reason": "outside the configured worktree root"}
        if helper is None and tree.lock is not None:
            return {**row, "reason": "portable worktree has a lock; resolve its owning job first"}
        repo = github_slug(checkout)
        if not repo:
            return {**row, "reason": "checkout origin is not a GitHub repository"}
        import github_read

        if reader is None:
            reader = github_read.GitHubReader(operation="github.worktree.retire_landed", include_archived=True)
        pulls = reader.paged_json(f"repos/{repo}/pulls", step_prefix="retirement_target_pulls",
                                  params={"state": "all", "head": f"{repo.split('/')[0]}:{tree.branch}"}) if tree.branch else []
        if not pulls:
            pulls = commit_pulls(reader, github_read, repo, tree.head)
        return {**row, **ownership_decision(reader, github_read, repo, pulls, [tree.branch] if tree.branch else [],
                                            tree=tree)}
    except UnpushedCommit as exc:
        return {**row, "reason": str(exc)}
    except Exception as exc:
        return {**row, "reason": f"retirement evidence unavailable: {type(exc).__name__}: {exc}"}


def issues_naming(reader, owner: str, branch: str) -> list[tuple[str, int]]:
    query = f'"{branch}" user:{owner} is:issue'
    result = reader.request("GET", "search/issues?" + urlencode({"q": query, "per_page": SEARCH_PAGE}),
                            step="landed_branch_claims", bucket="search").body
    items = result.get("items") if isinstance(result, dict) else None
    if (not isinstance(items, list) or result.get("incomplete_results") is not False
            or result.get("total_count") != len(items)):
        raise ValueError(f"issue search for {branch} is incomplete")
    return [(item["repository_url"].split("/repos/", 1)[1], item["number"]) for item in items]


def claims_on(reader, github_read, records: list[tuple[str, int]], branches: set[str]) -> str:
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
        body = (issue or {}).get("body") or ""
        custody = re.sub(r"<!--.*?-->", "", github_plan_claim.unquoted_text(body), flags=re.S)
        names_branch = any(re.search(
            rf"(?:^|[\s`'\"(<\[,;:*]){re.escape(branch)}(?=$|[\s`'\")>\],;:*?]|\.(?:\s|$))", custody,
        ) for branch in branches)
        conflicts, _ = github_plan_claim.discussion_evidence(body, comments, ANY_CLAIM,
                                                             ended_claims=released)
        found += [json.dumps(c["record"]) if "record" in c else c.get("text", "") for c in conflicts
                  if names_branch or not isinstance(c.get("record"), dict)
                  or not isinstance(c["record"].get("branch"), str) or c["record"]["branch"] in branches]
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
        rows = landed_decisions(checkout, head["ref"], head["sha"], root, pr_commits=pr_commits)
        ownership = ownership_decision(reader, github_read, repo, [pull], [row["branch"] for row in rows])
    except (github_read.GitHubReadError, github_read.GitHubReadShapeError, ValueError, TypeError, KeyError) as exc:
        raise LookupError(f"PR commits or claims unavailable; nothing is retired: {exc}") from exc
    if ownership["decision"] == "kept":
        for row in rows:
            if row["decision"] == "candidate":
                row.update(ownership)
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
        ownership = ownership_decision(reader, github_read, repo, [], [branch])
    except (github_read.GitHubReadError, github_read.GitHubReadShapeError, ValueError, TypeError, KeyError) as exc:
        raise LookupError(f"claims naming {branch} unavailable; nothing is retired: {exc}") from exc
    rows = landed_decisions(checkout, branch, tip, root, claims=ownership.get("reason", ""))
    return {}, keep_fresh_starts(rows, dt.datetime.now(dt.timezone.utc))


def repository_rows(reader: Any, github_read: Any, repo: str, checkout: Path, root: Path, *,
                    preserve: set[Path] | frozenset[Path] = frozenset(),
                    deadline: float | None = None) -> Iterator[dict]:
    trees = registered(checkout)
    _, helper = dev_worktree.configuration()
    if deadline is not None:
        random.shuffle(trees)
    tip = default_tracking_tip(checkout)
    default_starts = first_parent_history(checkout)
    closed_pulls = {}

    def decisions_for_trees() -> Iterator[dict]:
        for tree in trees:
            row: dict[str, Any] = {"path": str(tree.path), "branch": tree.branch, "head": tree.head, "decision": "kept"}
            try:
                if any(is_under(path, tree.path) for path in preserve):
                    row["reason"] = "source checkout or calling session uses this tree"
                elif not is_under(tree.path, root):
                    row["reason"] = "outside the configured worktree root"
                elif helper is None and tree.lock is not None:
                    row["reason"] = "portable worktree has a lock; resolve its owning job first"
                elif deadline is not None and time.monotonic() >= deadline:
                    row["reason"] = "creation cleanup time budget exhausted; checked on the next start"
                elif (fresh := keep_fresh_starts([{**row, "decision": "candidate"}], dt.datetime.now(dt.timezone.utc))[0])["decision"] == "kept":
                    row = fresh
                elif tree.branch is None and tree.head in default_starts:
                    row["reason"] = "detached HEAD may be a fresh start on the default branch"
                else:
                    pulls = reader.paged_json(f"repos/{repo}/pulls", step_prefix="retirement_repository_pulls",
                                              params={"state": "all", "head": f"{repo.split('/')[0]}:{tree.branch}"}) if tree.branch else []
                    if not pulls and (not tree.branch or not tip or not contained(checkout, tree.head, tip)):
                        pulls = commit_pulls(reader, github_read, repo, tree.head)
                    open_pulls = [pull for pull in pulls if not finished_pull(pull)]
                    if open_pulls:
                        row.update(ownership_decision(reader, github_read, repo, pulls,
                                                      [tree.branch] if tree.branch else [], tree=tree))
                        if row["decision"] == "candidate":
                            row["published_in"] = sorted(pull["number"] for pull in open_pulls)
                    else:
                        if pulls:
                            pull = max(pulls, key=lambda item: item.get("closed_at") or "")
                            if pull["number"] not in closed_pulls:
                                closed_pulls[pull["number"]] = pull_rows(reader, github_read, repo, pull["number"], checkout, root)[1]
                            decisions = closed_pulls[pull["number"]]
                        elif tree.branch:
                            _, decisions = branch_rows(reader, github_read, repo, tree.branch, checkout, root)
                        else:
                            decisions = []
                        row = next((dict(decision) for decision in decisions if decision["path"] == str(tree.path)),
                                   {**row, "reason": "no covered worktree found; use --branch or --pr with exact provenance"})
                        row = keep_fresh_starts([row], dt.datetime.now(dt.timezone.utc))[0]
            except UnpushedCommit as exc:
                row.update(decision="kept", reason=str(exc))
            except Exception as exc:
                row.update(decision="kept", reason=f"retirement evidence unavailable: {type(exc).__name__}: {exc}")
            yield row

    return decisions_for_trees()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guarded retirement of finished repository worktrees.")
    parser.add_argument("--repo", help="OWNER/REPO of the landed work")
    landed = parser.add_mutually_exclusive_group(required=True)
    landed.add_argument("--all", action="store_true", help="check every linked worktree of this repository")
    landed.add_argument("--pr", type=int)
    landed.add_argument("--branch", help="a task branch that never had a PR and is in the default branch")
    landed.add_argument("--path", type=Path, help="qualify only this exact linked worktree")
    parser.add_argument("--checkout", required=True, type=Path, help="any local checkout of that repository")
    parser.add_argument("--apply", action="store_true", help="retire; the default only previews")
    parser.add_argument("--budget-seconds", type=float, help="stop starting checks after this many seconds; finish any guarded retirement in progress")
    parser.add_argument("--preserve", type=Path, action="append", default=[], help="retain an explicitly preserved path")
    args = parser.parse_args(argv)
    if args.budget_seconds is not None and (args.budget_seconds <= 0 or not math.isfinite(args.budget_seconds)):
        return refuse("budget-seconds must be positive and finite")
    deadline = None
    retirement_deadline = None
    if args.budget_seconds is not None:
        deadline = time.monotonic() + args.budget_seconds
        retirement_deadline = deadline - min(5.0, args.budget_seconds / 2)
    try:
        root, _helper = dev_worktree.configuration()
    except (dev_worktree.Refused, OSError, json.JSONDecodeError) as exc:
        return refuse(f"worktree configuration: {exc}")
    checkout_slug = github_slug(args.checkout)
    if not checkout_slug or (args.repo and not same_repository(checkout_slug, args.repo)):
        return refuse(f"checkout origin is {checkout_slug or 'not GitHub'}; expected {args.repo or 'a GitHub repository'}")
    repo = checkout_slug
    import github_read

    reader = github_read.GitHubReader(operation="github.worktree.retire_landed", include_archived=True,
                                      deadline_at=time.time() + args.budget_seconds if args.budget_seconds else None)
    try:
        if args.path:
            summary = {}
            rows = [target_decision(args.path, args.checkout, preserve=args.preserve, reader=reader)]
        elif args.all:
            summary = {}
            rows = repository_rows(
                reader,
                github_read,
                repo,
                args.checkout,
                root,
                preserve={
                    args.checkout.resolve(),
                    Path.cwd().resolve(),
                    Path(__file__).resolve(),
                    *(path.resolve() for path in args.preserve),
                },
                deadline=retirement_deadline,
            )
        elif args.branch:
            summary, rows = branch_rows(reader, github_read, repo, args.branch, args.checkout, root)
        else:
            summary, rows = pull_rows(reader, github_read, repo, args.pr, args.checkout, root)
    except (LookupError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        return refuse(str(exc))
    rows = retire(rows, apply=args.apply, deadline=retirement_deadline)
    strays = []
    if args.all:
        try:
            strays = sweep_strays(args.checkout, root, apply=args.apply, deadline=deadline)
        except Exception as exc:
            strays = [{"path": str(root), "decision": "kept", "reason": f"stray cleanup unavailable: {type(exc).__name__}: {exc}"}]
    passed = {"retired", "would_retire"}
    ok = not args.path or len(rows) == 1 and rows[0]["decision"] in passed
    print(json.dumps({"ok": bool(ok), **summary, "worktrees": rows, "strays": strays}, indent=2), flush=True)
    return 0 if ok else 2

if __name__ == "__main__":
    sys.exit(main())
