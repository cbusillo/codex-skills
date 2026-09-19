#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Safely reconcile a runtime-bound checkout after a confirmed merge."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
SUCCESS_STATUSES = frozenset({"synchronized", "already_current", "not_applicable"})
LANDING_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
UNSAFE_GIT_ENVIRONMENT = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_QUARANTINE_PATH",
        "GIT_WORK_TREE",
    }
)


class GitCommandError(RuntimeError):
    def __init__(self, operation: str, stderr: str = "") -> None:
        super().__init__(operation)
        self.operation = operation
        self.stderr = stderr.strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fast-forward the active runtime checkout after a confirmed repository landing.",
    )
    parser.add_argument(
        "--merged-worktree",
        type=Path,
        required=True,
        help="A worktree from the repository whose default branch landed.",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Expected repository in OWNER/REPO form.",
    )
    parser.add_argument(
        "--landing-sha",
        required=True,
        help="The full Git commit SHA confirmed as landed on the repository default branch.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        receipt = reconcile_runtime_checkout(args.merged_worktree, args.repo, args.landing_sha)
    except GitCommandError as exc:
        receipt = finish(base_receipt(args.landing_sha), "failed", "unexpected_git_error")
        receipt["expected_repo"] = args.repo
        receipt["failed_operation"] = exc.operation
    except OSError as exc:
        receipt = finish(base_receipt(args.landing_sha), "failed", "unexpected_os_error")
        receipt["expected_repo"] = args.repo
        receipt["failed_operation"] = type(exc).__name__
    print(json.dumps(receipt, indent=2, sort_keys=True))
    if receipt["status"] in SUCCESS_STATUSES:
        return 0
    if receipt["status"] == "blocked":
        return 2
    return 1


def reconcile_runtime_checkout(
    merged_worktree: Path,
    expected_repo: str,
    landing_sha: str,
) -> dict[str, Any]:
    receipt = base_receipt(landing_sha)
    receipt["expected_repo"] = expected_repo
    if not REPOSITORY_PATTERN.fullmatch(expected_repo):
        return finish(receipt, "failed", "invalid_repository")
    if not LANDING_SHA_PATTERN.fullmatch(landing_sha):
        return finish(receipt, "failed", "invalid_landing_sha")

    try:
        source_root = git_root(merged_worktree)
        source_common_dir = git_common_dir(source_root)
        source_head = git_text(source_root, "rev-parse", "HEAD")
        default_branch = resolve_default_branch(source_root)
        helper_relative_path = helper_path_relative_to(source_root)
    except (GitCommandError, OSError, ValueError, json.JSONDecodeError):
        return finish(receipt, "failed", "invalid_merged_worktree")

    receipt.update(
        {
            "base_branch": default_branch,
            "source_head": source_head,
            "helper_path": helper_relative_path.as_posix(),
        }
    )

    runtime_path, home_source = resolve_runtime_skills_path()
    receipt["runtime_home_source"] = home_source
    if not runtime_path.exists():
        return finish(receipt, "not_applicable", "runtime_path_missing", applicable=False)

    try:
        resolved_runtime_path = runtime_path.resolve(strict=True)
    except OSError:
        return finish(receipt, "failed", "runtime_path_unavailable")

    runtime_root_result = run_git(
        resolved_runtime_path,
        "rev-parse",
        "--show-toplevel",
        check=False,
    )
    if runtime_root_result.returncode != 0:
        stderr = runtime_root_result.stderr.decode(errors="replace")
        if "not a git repository" in stderr.lower():
            return finish(receipt, "not_applicable", "runtime_path_not_git", applicable=False)
        return finish(receipt, "failed", "runtime_git_unavailable")

    runtime_root = Path(runtime_root_result.stdout.decode().strip()).resolve()
    try:
        runtime_common_dir = git_common_dir(runtime_root)
    except GitCommandError:
        return finish(receipt, "failed", "runtime_git_unavailable")

    if runtime_common_dir != source_common_dir:
        return finish(receipt, "not_applicable", "runtime_repo_mismatch", applicable=False)

    receipt["binding"] = "shared_git_common_dir"
    receipt["applicable"] = True

    try:
        with reconciliation_lock(runtime_common_dir):
            blockers = runtime_blockers(runtime_root, default_branch)
            if blockers:
                receipt["blockers"] = blockers
                receipt["before_sha"] = safe_git_text(runtime_root, "rev-parse", "HEAD")
                receipt["after_sha"] = receipt["before_sha"]
                return finish(receipt, "blocked", blockers[0])

            if git_has_url_rewrite(runtime_root):
                receipt["blockers"] = ["runtime_url_rewrite_configured"]
                receipt["before_sha"] = safe_git_text(runtime_root, "rev-parse", "HEAD")
                receipt["after_sha"] = receipt["before_sha"]
                return finish(receipt, "blocked", "runtime_url_rewrite_configured")

            try:
                origin_url = git_text(runtime_root, "config", "--get", "remote.origin.url")
                origin_repo = repository_from_remote_url(origin_url)
            except (GitCommandError, ValueError):
                return finish(receipt, "failed", "runtime_origin_unavailable")
            receipt["origin_repo"] = origin_repo
            if origin_repo.casefold() != expected_repo.casefold():
                receipt["blockers"] = ["runtime_origin_repo_mismatch"]
                receipt["before_sha"] = safe_git_text(runtime_root, "rev-parse", "HEAD")
                receipt["after_sha"] = receipt["before_sha"]
                return finish(receipt, "blocked", "runtime_origin_repo_mismatch")

            before_sha = git_text(runtime_root, "rev-parse", "HEAD")
            receipt["before_sha"] = before_sha
            remote_ref = f"refs/remotes/origin/{default_branch}"
            try:
                git_text(
                    runtime_root,
                    "fetch",
                    "--no-tags",
                    origin_url,
                    f"+refs/heads/{default_branch}:{remote_ref}",
                )
            except GitCommandError:
                receipt["after_sha"] = safe_git_text(runtime_root, "rev-parse", "HEAD")
                return finish(receipt, "retryable", "fetch_failed")

            blockers = runtime_blockers(runtime_root, default_branch)
            if blockers:
                receipt["blockers"] = blockers
                receipt["after_sha"] = safe_git_text(runtime_root, "rev-parse", "HEAD")
                return finish(receipt, "blocked", "runtime_changed_during_reconciliation")

            fetched_sha = git_text(runtime_root, "rev-parse", remote_ref)
            current_sha = git_text(runtime_root, "rev-parse", "HEAD")
            receipt["fetched_sha"] = fetched_sha

            if not git_object_exists(runtime_root, landing_sha):
                receipt["after_sha"] = current_sha
                return finish(receipt, "retryable", "landing_sha_unavailable")
            if not git_is_ancestor(runtime_root, landing_sha, fetched_sha):
                receipt["after_sha"] = current_sha
                return finish(receipt, "failed", "landing_not_on_default_tip")
            if not git_is_first_parent(runtime_root, landing_sha, fetched_sha):
                receipt["after_sha"] = current_sha
                return finish(receipt, "failed", "landing_not_on_first_parent")
            receipt["landing_relation"] = "first_parent_ancestor"
            landing_default_branch = default_branch_at_revision(source_root, landing_sha)
            if landing_default_branch and landing_default_branch != default_branch:
                receipt["after_sha"] = current_sha
                return finish(receipt, "failed", "landing_default_branch_mismatch")

            executing_helper = Path(__file__).read_bytes()
            receipt["helper_sha256"] = hashlib.sha256(executing_helper).hexdigest()
            try:
                landing_helper = git_blob_bytes(
                    source_root,
                    landing_sha,
                    helper_relative_path,
                )
                tip_helper = git_blob_bytes(
                    source_root,
                    fetched_sha,
                    helper_relative_path,
                )
                landing_helper_oid = git_text(
                    source_root,
                    "rev-parse",
                    f"{landing_sha}:{helper_relative_path.as_posix()}",
                )
                tip_helper_oid = git_text(
                    source_root,
                    "rev-parse",
                    f"{fetched_sha}:{helper_relative_path.as_posix()}",
                )
            except GitCommandError:
                receipt["after_sha"] = current_sha
                return finish(receipt, "failed", "landed_helper_missing")

            receipt["helper_blob_oid"] = landing_helper_oid
            receipt["helper_tip_blob_oid"] = tip_helper_oid
            canonical_executing_helper = canonical_script_bytes(executing_helper)
            receipt["helper_landing_verified"] = (
                canonical_executing_helper == canonical_script_bytes(landing_helper)
            )
            receipt["helper_tip_verified"] = (
                canonical_executing_helper == canonical_script_bytes(tip_helper)
            )
            if not receipt["helper_landing_verified"]:
                receipt["after_sha"] = current_sha
                return finish(receipt, "failed", "helper_landing_source_mismatch")
            if not receipt["helper_tip_verified"]:
                receipt["after_sha"] = current_sha
                return finish(receipt, "failed", "helper_tip_source_mismatch")
            receipt["helper_source_verified"] = True

            if not git_is_ancestor(runtime_root, current_sha, fetched_sha):
                receipt["after_sha"] = current_sha
                if git_is_ancestor(runtime_root, fetched_sha, current_sha):
                    return finish(receipt, "blocked", "runtime_ahead")
                return finish(receipt, "blocked", "runtime_diverged")

            if current_sha == fetched_sha:
                receipt["after_sha"] = current_sha
                if not runtime_binding_matches(runtime_path, runtime_common_dir):
                    return finish(receipt, "failed", "postcondition_runtime_binding_changed")
                return finish(receipt, "already_current", "runtime_current")

            pre_merge_blockers = runtime_blockers(runtime_root, default_branch)
            pre_merge_sha = git_text(runtime_root, "rev-parse", "HEAD")
            if pre_merge_blockers or pre_merge_sha != current_sha:
                receipt["blockers"] = pre_merge_blockers or ["runtime_head_changed"]
                receipt["after_sha"] = pre_merge_sha
                return finish(receipt, "blocked", "runtime_changed_before_fast_forward")

            try:
                git_text(
                    runtime_root,
                    "merge",
                    "--ff-only",
                    "--no-autostash",
                    "--no-overwrite-ignore",
                    fetched_sha,
                )
            except GitCommandError:
                receipt["after_sha"] = safe_git_text(runtime_root, "rev-parse", "HEAD")
                receipt["runtime_mutated"] = receipt["after_sha"] != before_sha
                return finish(receipt, "failed", "fast_forward_failed")

            after_sha = git_text(runtime_root, "rev-parse", "HEAD")
            receipt["after_sha"] = after_sha
            receipt["runtime_mutated"] = after_sha != before_sha
            if after_sha != fetched_sha:
                return finish(receipt, "failed", "postcondition_head_mismatch")
            post_blockers = runtime_blockers(runtime_root, default_branch)
            if post_blockers:
                receipt["blockers"] = post_blockers
                return finish(receipt, "failed", "postcondition_runtime_not_clean")
            if not runtime_binding_matches(runtime_path, runtime_common_dir):
                return finish(receipt, "failed", "postcondition_runtime_binding_changed")
            if not git_is_first_parent(runtime_root, landing_sha, after_sha):
                return finish(receipt, "failed", "postcondition_landing_missing")
            return finish(receipt, "synchronized", "runtime_fast_forwarded")
    except GitCommandError as exc:
        receipt["failed_operation"] = exc.operation
        receipt["after_sha"] = safe_git_text(runtime_root, "rev-parse", "HEAD")
        receipt["runtime_mutated"] = receipt["after_sha"] != receipt["before_sha"]
        return finish(receipt, "failed", "unexpected_git_error")
    except OSError as exc:
        receipt["failed_operation"] = type(exc).__name__
        receipt["after_sha"] = safe_git_text(runtime_root, "rev-parse", "HEAD")
        receipt["runtime_mutated"] = receipt["after_sha"] != receipt["before_sha"]
        return finish(receipt, "failed", "unexpected_os_error")


def base_receipt(landing_sha: str) -> dict[str, Any]:
    return {
        "ok": False,
        "schema_version": SCHEMA_VERSION,
        "kind": "runtime_checkout_reconciliation",
        "status": "failed",
        "reason_code": "uninitialized",
        "applicable": None,
        "binding": None,
        "runtime_home_source": None,
        "base_branch": None,
        "landing_sha": landing_sha,
        "expected_repo": None,
        "origin_repo": None,
        "landing_relation": None,
        "source_head": None,
        "before_sha": None,
        "fetched_sha": None,
        "after_sha": None,
        "helper_path": None,
        "helper_sha256": None,
        "helper_blob_oid": None,
        "helper_tip_blob_oid": None,
        "helper_landing_verified": False,
        "helper_tip_verified": False,
        "helper_source_verified": False,
        "runtime_mutated": False,
        "failed_operation": None,
        "blockers": [],
    }


def finish(
    receipt: dict[str, Any],
    status: str,
    reason_code: str,
    *,
    applicable: bool | None = None,
) -> dict[str, Any]:
    receipt["status"] = status
    receipt["reason_code"] = reason_code
    receipt["ok"] = status in SUCCESS_STATUSES
    if applicable is not None:
        receipt["applicable"] = applicable
    return receipt


def resolve_runtime_skills_path() -> tuple[Path, str]:
    code_home = os.environ.get("CODE_HOME")
    if code_home:
        return Path(code_home).expanduser() / "skills", "CODE_HOME"
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "skills", "CODEX_HOME"
    return Path.home() / ".code" / "skills", "HOME/.code"


def repository_from_remote_url(remote_url: str) -> str:
    value = remote_url.strip()
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme:
        path = parsed.path
    elif re.match(r"^[^/\\]+@[^:]+:", value):
        path = value.split(":", 1)[1]
    else:
        path = value
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    if len(parts) < 2:
        raise ValueError("Remote URL does not identify OWNER/REPO")
    owner = parts[-2]
    repo = parts[-1][:-4] if parts[-1].endswith(".git") else parts[-1]
    candidate = f"{owner}/{repo}"
    if not REPOSITORY_PATTERN.fullmatch(candidate):
        raise ValueError("Remote URL does not identify OWNER/REPO")
    return candidate


def resolve_default_branch(source_root: Path) -> str:
    remote_head = run_git(
        source_root,
        "symbolic-ref",
        "--quiet",
        "--short",
        "refs/remotes/origin/HEAD",
        check=False,
    )
    if remote_head.returncode == 0:
        value = remote_head.stdout.decode().strip()
        _, separator, branch = value.partition("/")
        if separator and branch:
            return branch
    metadata_path = source_root / ".github" / "github.json"
    if metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text())
        configured = metadata.get("defaultBranch")
        if isinstance(configured, str) and configured.strip():
            branch = configured.strip()
            git_text(source_root, "check-ref-format", "--branch", branch)
            return branch
    raise ValueError("Unable to determine default branch")


def default_branch_at_revision(source_root: Path, revision: str) -> str | None:
    proc = run_git(
        source_root,
        "cat-file",
        "blob",
        f"{revision}:.github/github.json",
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        metadata = json.loads(proc.stdout.decode())
    except json.JSONDecodeError as exc:
        raise GitCommandError("landing_metadata") from exc
    configured = metadata.get("defaultBranch")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return None


def helper_path_relative_to(source_root: Path) -> Path:
    helper_path = Path(__file__).resolve()
    return helper_path.relative_to(source_root.resolve())


def runtime_blockers(runtime_root: Path, default_branch: str) -> list[str]:
    blockers: list[str] = []
    branch = current_branch(runtime_root)
    if branch is None:
        blockers.append("runtime_detached")
    elif branch != default_branch:
        blockers.append("runtime_wrong_branch")
    if git_has_hidden_index_state(runtime_root):
        blockers.append("runtime_hidden_index_state")
    if git_text(runtime_root, "status", "--porcelain=v1", "--untracked-files=all"):
        blockers.append("runtime_dirty")
    return blockers


def current_branch(repo: Path) -> str | None:
    proc = run_git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if proc.returncode == 0:
        return proc.stdout.decode().strip()
    if proc.returncode == 1:
        return None
    raise GitCommandError("symbolic-ref", proc.stderr.decode(errors="replace"))


def git_root(path: Path) -> Path:
    return Path(git_text(path, "rev-parse", "--show-toplevel")).resolve()


def git_common_dir(repo: Path) -> Path:
    return Path(
        git_text(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve()


def git_object_exists(repo: Path, revision: str) -> bool:
    proc = run_git(repo, "cat-file", "-e", f"{revision}^{{commit}}", check=False)
    return proc.returncode == 0


def git_is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    proc = run_git(repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise GitCommandError("merge-base", proc.stderr.decode(errors="replace"))


def git_has_url_rewrite(repo: Path) -> bool:
    proc = run_git(
        repo,
        "config",
        "--get-regexp",
        r"^url\..*\.insteadof$",
        check=False,
    )
    if proc.returncode == 0:
        return bool(proc.stdout.strip())
    if proc.returncode == 1:
        return False
    raise GitCommandError("config", proc.stderr.decode(errors="replace"))


def git_has_hidden_index_state(repo: Path) -> bool:
    entries = git_text(repo, "ls-files", "-v")
    for entry in entries.splitlines():
        if not entry:
            continue
        marker = entry[0]
        if marker == "S" or marker.islower():
            return True
    return False


def git_is_first_parent(repo: Path, commit: str, tip: str) -> bool:
    first_parent_history = git_text(repo, "rev-list", "--first-parent", tip)
    return commit in first_parent_history.splitlines()


def git_blob_bytes(repo: Path, revision: str, path: Path) -> bytes:
    return run_git(repo, "cat-file", "blob", f"{revision}:{path.as_posix()}").stdout


def canonical_script_bytes(content: bytes) -> bytes:
    return content.replace(b"\r\n", b"\n")


def runtime_binding_matches(runtime_path: Path, expected_common_dir: Path) -> bool:
    try:
        resolved_runtime_path = runtime_path.resolve(strict=True)
        runtime_root = git_root(resolved_runtime_path)
        return git_common_dir(runtime_root) == expected_common_dir
    except (GitCommandError, OSError):
        return False


def git_text(repo: Path, *args: str) -> str:
    proc = run_git(repo, *args)
    return proc.stdout.decode().strip()


def safe_git_text(repo: Path, *args: str) -> str | None:
    try:
        return git_text(repo, *args)
    except GitCommandError:
        return None


def run_git(
    repo: Path,
    *args: str,
    check: bool = True,
    input_data: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    env = sanitized_git_environment()
    try:
        proc = subprocess.run(
            [
                "git",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "merge.autoStash=false",
                "-C",
                str(repo),
                *args,
            ],
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitCommandError(args[0] if args else "git", "Git command timed out") from exc
    if check and proc.returncode != 0:
        raise GitCommandError(args[0] if args else "git", proc.stderr.decode(errors="replace"))
    return proc


def sanitized_git_environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key in UNSAFE_GIT_ENVIRONMENT:
            env.pop(key, None)
        elif key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
            env.pop(key, None)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


@contextlib.contextmanager
def reconciliation_lock(common_dir: Path) -> Iterator[None]:
    lock_path = common_dir / "every-code-runtime-reconciliation.lock"
    with lock_path.open("a+b") as lock_file:
        if os.name == "posix":
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        elif os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            raise OSError("Unsupported platform for runtime reconciliation locking")
        try:
            yield
        finally:
            if os.name == "posix":
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            elif os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


if __name__ == "__main__":
    raise SystemExit(main())
