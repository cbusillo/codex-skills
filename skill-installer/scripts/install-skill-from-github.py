#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Install a skill from a GitHub repo path into the active skills directory."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import zipfile

from github_utils import github_request


DEFAULT_REF = "main"
FULL_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
SHORT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,39}$")
GITHUB_API_ROOT = "https://api.github.com"


@dataclass
class Args:
    url: str | None = None
    repo: str | None = None
    path: list[str] | None = None
    ref: str = DEFAULT_REF
    dest: str | None = None
    name: str | None = None
    method: str = "auto"


@dataclass(frozen=True)
class Source:
    owner: str
    repo: str
    ref: str | None
    paths: tuple[str, ...]
    url_ref_path: tuple[str, ...] | None = None
    url_kind: str | None = None


@dataclass(frozen=True)
class RepositoryIdentity:
    full_name: str
    repository_id: int


@dataclass(frozen=True)
class ResolvedSource:
    repository: RepositoryIdentity
    requested_ref: str
    resolved_sha: str
    paths: tuple[str, ...]


class InstallError(Exception):
    pass


class RefNotFoundError(InstallError):
    pass


class ShortShaError(InstallError):
    pass


class SourceDriftError(InstallError):
    pass


class DownloadError(InstallError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _runtime_home() -> str:
    if os.environ.get("CODE_HOME"):
        return os.environ["CODE_HOME"]
    if os.environ.get("CODEX_HOME"):
        return os.environ["CODEX_HOME"]
    code_home = os.path.expanduser("~/.code")
    if os.path.isdir(os.path.join(code_home, "skills")) or os.path.exists(
        os.path.join(code_home, "plans")
    ):
        return code_home
    return os.path.expanduser("~/.codex")


def _tmp_root() -> str:
    base = os.path.join(tempfile.gettempdir(), "codex")
    os.makedirs(base, exist_ok=True)
    return base


def _request(url: str) -> bytes:
    return github_request(url, "codex-skill-install")


def _request_json(
    url: str,
    context: str,
    *,
    missing_ok: bool = False,
) -> dict[str, object] | list[object] | None:
    try:
        payload = _request(url)
    except urllib.error.HTTPError as exc:
        if missing_ok and exc.code == 404:
            return None
        raise InstallError(f"{context}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise InstallError(f"{context}: {exc.reason}") from exc
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"{context}: unexpected GitHub response") from exc
    if not isinstance(data, (dict, list)):
        raise InstallError(f"{context}: unexpected GitHub response")
    return data


def _quoted_repo_path(owner: str, repo: str) -> str:
    return "/".join(
        (urllib.parse.quote(owner, safe=""), urllib.parse.quote(repo, safe=""))
    )


def _repo_api_url(owner: str, repo: str, suffix: str = "") -> str:
    base = f"{GITHUB_API_ROOT}/repos/{_quoted_repo_path(owner, repo)}"
    return f"{base}/{suffix}" if suffix else base


def _split_full_name(full_name: str) -> tuple[str, str]:
    owner, repo = full_name.split("/", 1)
    return owner, repo


def _validate_repository_component(value: str) -> None:
    if not value or value in (".", "..") or "/" in value or "\\" in value:
        raise InstallError("Invalid GitHub repository identity.")


def _assert_response_repository(
    data: dict[str, object], repository: RepositoryIdentity
) -> None:
    response_url = data.get("url")
    expected_prefix = f"{GITHUB_API_ROOT}/repos/{repository.full_name}/"
    if not isinstance(response_url, str) or not response_url.casefold().startswith(
        expected_prefix.casefold()
    ):
        raise SourceDriftError(
            "Repository identity changed while resolving the requested ref."
        )


def _resolve_repository_identity(owner: str, repo: str) -> RepositoryIdentity:
    _validate_repository_component(owner)
    _validate_repository_component(repo)
    data = _request_json(
        _repo_api_url(owner, repo),
        f"Unable to resolve repository {owner}/{repo}",
        missing_ok=True,
    )
    if data is None:
        raise InstallError(
            f"Repository not found or inaccessible: {owner}/{repo}. "
            "Set GITHUB_TOKEN or GH_TOKEN for private repositories."
        )
    if not isinstance(data, dict):
        raise InstallError("Unexpected GitHub repository response.")
    full_name = data.get("full_name")
    repository_id = data.get("id")
    if not isinstance(full_name, str) or not isinstance(repository_id, int):
        raise InstallError("Unexpected GitHub repository response.")
    requested_full_name = f"{owner}/{repo}"
    if full_name.casefold() != requested_full_name.casefold():
        raise SourceDriftError(
            f"Repository identity changed from {requested_full_name} to {full_name}."
        )
    return RepositoryIdentity(full_name=full_name, repository_id=repository_id)


def _verify_repository_identity(repository: RepositoryIdentity) -> None:
    owner, repo = _split_full_name(repository.full_name)
    data = _request_json(
        _repo_api_url(owner, repo),
        f"Unable to verify repository {repository.full_name}",
        missing_ok=True,
    )
    if data is None or not isinstance(data, dict):
        raise SourceDriftError(
            f"Repository identity changed after resolving {repository.full_name}."
        )
    if (
        data.get("full_name") != repository.full_name
        or data.get("id") != repository.repository_id
    ):
        raise SourceDriftError(
            f"Repository identity changed after resolving {repository.full_name}."
        )


def _get_exact_ref(
    repository: RepositoryIdentity, qualified_ref: str
) -> tuple[str, str] | None:
    if not qualified_ref.startswith("refs/"):
        raise InstallError("Internal error: GitHub ref must start with refs/.")
    owner, repo = _split_full_name(repository.full_name)
    ref_path = urllib.parse.quote(qualified_ref.removeprefix("refs/"), safe="/")
    data = _request_json(
        _repo_api_url(owner, repo, f"git/ref/{ref_path}"),
        f"Unable to resolve ref {qualified_ref}",
        missing_ok=True,
    )
    if data is None:
        return None
    if not isinstance(data, dict):
        raise InstallError(f"Unexpected GitHub response for ref {qualified_ref}.")
    _assert_response_repository(data, repository)
    if data.get("ref") != qualified_ref:
        raise InstallError(f"GitHub returned a different ref for {qualified_ref}.")
    target = data.get("object")
    if not isinstance(target, dict):
        raise InstallError(f"Unexpected GitHub response for ref {qualified_ref}.")
    object_type = target.get("type")
    object_sha = target.get("sha")
    if not isinstance(object_type, str) or not isinstance(object_sha, str):
        raise InstallError(f"Unexpected GitHub response for ref {qualified_ref}.")
    return object_type, object_sha


def _get_tag_target(
    repository: RepositoryIdentity, tag_sha: str
) -> tuple[str, str]:
    owner, repo = _split_full_name(repository.full_name)
    data = _request_json(
        _repo_api_url(owner, repo, f"git/tags/{tag_sha}"),
        f"Unable to resolve annotated tag {tag_sha}",
    )
    if not isinstance(data, dict):
        raise InstallError(f"Unexpected GitHub response for tag {tag_sha}.")
    _assert_response_repository(data, repository)
    target = data.get("object")
    if not isinstance(target, dict):
        raise InstallError(f"Unexpected GitHub response for tag {tag_sha}.")
    object_type = target.get("type")
    object_sha = target.get("sha")
    if not isinstance(object_type, str) or not isinstance(object_sha, str):
        raise InstallError(f"Unexpected GitHub response for tag {tag_sha}.")
    return object_type, object_sha


def _validate_commit_sha(repository: RepositoryIdentity, sha: str) -> str:
    owner, repo = _split_full_name(repository.full_name)
    data = _request_json(
        _repo_api_url(owner, repo, f"git/commits/{sha}"),
        f"Unable to resolve commit {sha}",
        missing_ok=True,
    )
    if data is None:
        raise RefNotFoundError(f"Commit not found: {sha}")
    if not isinstance(data, dict):
        raise InstallError(f"Unexpected GitHub response for commit {sha}.")
    _assert_response_repository(data, repository)
    resolved_sha = data.get("sha")
    if not isinstance(resolved_sha, str) or not FULL_SHA_PATTERN.fullmatch(
        resolved_sha
    ):
        raise InstallError(f"GitHub returned an invalid commit SHA for {sha}.")
    if resolved_sha.casefold() != sha.casefold():
        raise SourceDriftError(f"GitHub resolved commit {sha} to a different object.")
    return resolved_sha.lower()


def _peel_ref_to_commit(
    repository: RepositoryIdentity,
    requested_ref: str,
    target: tuple[str, str],
) -> str:
    object_type, object_sha = target
    visited: set[str] = set()
    while object_type == "tag":
        if object_sha in visited or len(visited) >= 16:
            raise InstallError(f"Annotated tag cycle detected for {requested_ref}.")
        visited.add(object_sha)
        object_type, object_sha = _get_tag_target(repository, object_sha)
    if object_type != "commit":
        raise InstallError(
            f"Ref {requested_ref} resolves to {object_type}, not a commit."
        )
    return _validate_commit_sha(repository, object_sha)


def _resolve_requested_ref(
    repository: RepositoryIdentity, requested_ref: str
) -> str:
    if not requested_ref:
        raise InstallError("GitHub ref cannot be empty.")
    if any(part in ("", ".", "..") for part in requested_ref.split("/")):
        raise InstallError("GitHub ref contains an unsafe path segment.")
    if FULL_SHA_PATTERN.fullmatch(requested_ref):
        return _validate_commit_sha(repository, requested_ref)
    if requested_ref.startswith("refs/"):
        target = _get_exact_ref(repository, requested_ref)
        if target is None:
            raise RefNotFoundError(f"Ref not found: {requested_ref}")
        return _peel_ref_to_commit(repository, requested_ref, target)

    branch_ref = f"refs/heads/{requested_ref}"
    tag_ref = f"refs/tags/{requested_ref}"
    branch_target = _get_exact_ref(repository, branch_ref)
    tag_target = _get_exact_ref(repository, tag_ref)
    if branch_target is not None and tag_target is not None:
        raise InstallError(
            f"Ambiguous ref {requested_ref!r}: both {branch_ref} and {tag_ref} exist. "
            "Use an explicit refs/... name."
        )
    if branch_target is not None:
        return _peel_ref_to_commit(repository, branch_ref, branch_target)
    if tag_target is not None:
        return _peel_ref_to_commit(repository, tag_ref, tag_target)
    if SHORT_SHA_PATTERN.fullmatch(requested_ref):
        raise ShortShaError(
            "Short commit SHAs are not accepted. Use the full 40-character SHA."
        )
    raise RefNotFoundError(f"Ref not found: {requested_ref}")


def _path_type_at_commit(
    repository: RepositoryIdentity, path: str, resolved_sha: str
) -> str | None:
    _validate_relative_path(path)
    owner, repo = _split_full_name(repository.full_name)
    encoded_path = urllib.parse.quote(path, safe="/")
    query = urllib.parse.urlencode({"ref": resolved_sha})
    data = _request_json(
        _repo_api_url(owner, repo, f"contents/{encoded_path}") + f"?{query}",
        f"Unable to inspect path {path}",
        missing_ok=True,
    )
    if data is None:
        return None
    if isinstance(data, list):
        return "dir"
    path_type = data.get("type")
    if not isinstance(path_type, str):
        raise InstallError(f"Unexpected GitHub response for path {path}.")
    return path_type


def _parse_github_url(
    url: str, default_ref: str, explicit_paths: list[str] | None
) -> Source:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.casefold() != "github.com":
        raise InstallError("Only GitHub URLs are supported.")
    parts: list[str] = []
    for raw_part in parsed.path.split("/"):
        if not raw_part:
            continue
        parts.extend(
            part for part in urllib.parse.unquote(raw_part).split("/") if part
        )
    if any(part in (".", "..") for part in parts):
        raise InstallError("GitHub URL contains an unsafe path segment.")
    if len(parts) < 2:
        raise InstallError("Invalid GitHub URL.")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if len(parts) > 2 and parts[2] in ("tree", "blob"):
        ref_path = tuple(parts[3:])
        if not ref_path:
            raise InstallError("GitHub URL missing ref or path.")
        if explicit_paths is not None:
            return Source(
                owner=owner,
                repo=repo,
                ref=None,
                paths=tuple(explicit_paths),
                url_ref_path=ref_path,
                url_kind=parts[2],
            )
        return Source(
            owner=owner,
            repo=repo,
            ref=None,
            paths=(),
            url_ref_path=ref_path,
            url_kind=parts[2],
        )

    url_path = "/".join(parts[2:])
    paths = tuple(explicit_paths) if explicit_paths is not None else ()
    if not paths and url_path:
        paths = (url_path,)
    return Source(owner=owner, repo=repo, ref=default_ref, paths=paths)


def _resolve_source_request(args: Args) -> Source:
    if args.url:
        return _parse_github_url(args.url, args.ref, args.path)
    if not args.repo:
        raise InstallError("Provide --repo or --url.")
    if "://" in args.repo:
        return _parse_github_url(args.repo, args.ref, args.path)
    repo_parts = args.repo.split("/")
    if len(repo_parts) != 2 or not all(repo_parts):
        raise InstallError("--repo must be in owner/repo format.")
    if not args.path:
        raise InstallError("Missing --path for --repo.")
    return Source(
        owner=repo_parts[0],
        repo=repo_parts[1],
        ref=args.ref,
        paths=tuple(args.path),
    )


def _resolve_url_ref_and_path(
    repository: RepositoryIdentity,
    ref_path: tuple[str, ...],
    explicit_paths: tuple[str, ...],
    url_kind: str | None,
) -> tuple[str, str, tuple[str, ...]]:
    if not explicit_paths and len(ref_path) < 2:
        raise InstallError(
            "GitHub tree/blob URL must include both a ref and a skill path."
        )
    matches: list[tuple[str, str, tuple[str, ...]]] = []
    final_split = len(ref_path) + 1 if explicit_paths else len(ref_path)
    for split_index in range(1, final_split):
        requested_ref = "/".join(ref_path[:split_index])
        try:
            resolved_sha = _resolve_requested_ref(repository, requested_ref)
        except (RefNotFoundError, ShortShaError):
            continue
        if explicit_paths:
            matches.append((requested_ref, resolved_sha, explicit_paths))
            continue
        path = "/".join(ref_path[split_index:])
        path_type = _path_type_at_commit(repository, path, resolved_sha)
        expected_type = "dir" if url_kind == "tree" else "file"
        if path_type == expected_type:
            matches.append((requested_ref, resolved_sha, (path,)))
    if not matches:
        raise InstallError(
            "Unable to resolve the GitHub URL ref and skill path. "
            "Use --repo, --ref, and --path to specify them explicitly."
        )
    if len(matches) > 1:
        raise InstallError(
            "GitHub URL matches multiple ref/path combinations. "
            "Use --repo, --ref, and --path to specify them explicitly."
        )
    return matches[0]


def _resolve_source(source: Source) -> ResolvedSource:
    repository = _resolve_repository_identity(source.owner, source.repo)
    if source.url_ref_path is not None:
        requested_ref, resolved_sha, paths = _resolve_url_ref_and_path(
            repository,
            source.url_ref_path,
            source.paths,
            source.url_kind,
        )
    else:
        if source.ref is None:
            raise InstallError("Missing GitHub ref.")
        requested_ref = source.ref
        resolved_sha = _resolve_requested_ref(repository, requested_ref)
        paths = source.paths
    if not paths:
        raise InstallError("No skill paths provided.")
    _verify_repository_identity(repository)
    return ResolvedSource(
        repository=repository,
        requested_ref=requested_ref,
        resolved_sha=resolved_sha,
        paths=paths,
    )


def _download_repo_zip(
    repository: RepositoryIdentity,
    resolved_sha: str,
    paths: tuple[str, ...],
    dest_dir: str,
) -> str:
    owner, repo = _split_full_name(repository.full_name)
    zip_url = (
        "https://codeload.github.com/"
        f"{_quoted_repo_path(owner, repo)}/zip/{resolved_sha}"
    )
    zip_path = os.path.join(dest_dir, "repo.zip")
    try:
        payload = _request(zip_url)
    except urllib.error.HTTPError as exc:
        raise DownloadError(
            f"Download failed: HTTP {exc.code}", status_code=exc.code
        ) from exc
    except urllib.error.URLError as exc:
        raise DownloadError(f"Download failed: {exc.reason}") from exc
    with open(zip_path, "wb") as file_handle:
        file_handle.write(payload)
    try:
        with zipfile.ZipFile(zip_path, "r") as zip_file:
            top_levels = {
                name.split("/")[0] for name in zip_file.namelist() if name
            }
    except (OSError, zipfile.BadZipFile) as exc:
        raise DownloadError("Downloaded archive was invalid.") from exc
    if not top_levels:
        raise DownloadError("Downloaded archive was empty.")
    if len(top_levels) != 1:
        raise DownloadError("Unexpected archive layout.")
    top_level = next(iter(top_levels))
    if not top_level.casefold().endswith(f"-{resolved_sha}".casefold()):
        raise SourceDriftError(
            "Downloaded archive did not match the resolved commit."
        )
    archive_roots = tuple(
        top_level
        if posixpath.normpath(path) == "."
        else f"{top_level}/{posixpath.normpath(path).strip('/')}"
        for path in paths
    )
    try:
        with zipfile.ZipFile(zip_path, "r") as zip_file:
            _safe_extract_zip(zip_file, dest_dir, archive_roots)
    except (OSError, zipfile.BadZipFile) as exc:
        raise DownloadError("Downloaded archive was invalid.") from exc
    return os.path.join(dest_dir, top_level)


def _run_git(args: list[str]) -> str:
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise InstallError(result.stderr.strip() or "Git command failed.")
    return result.stdout.strip()


def _git_sparse_checkout(
    repo_url: str, resolved_sha: str, paths: tuple[str, ...], dest_dir: str
) -> str:
    repo_dir = os.path.join(dest_dir, "repo")
    _run_git(["git", "init", "--quiet", repo_dir])
    _run_git(["git", "-C", repo_dir, "remote", "add", "origin", repo_url])
    _run_git(
        [
            "git",
            "-C",
            repo_dir,
            "fetch",
            "--filter=blob:none",
            "--depth",
            "1",
            "--no-tags",
            "origin",
            resolved_sha,
        ]
    )
    _run_git(["git", "-C", repo_dir, "sparse-checkout", "init", "--cone"])
    _run_git(
        [
            "git",
            "-C",
            repo_dir,
            "sparse-checkout",
            "set",
            "--cone",
            "--skip-checks",
            "--",
            *paths,
        ]
    )
    _run_git(["git", "-C", repo_dir, "checkout", "--detach", "FETCH_HEAD"])
    checked_out_sha = _run_git(["git", "-C", repo_dir, "rev-parse", "HEAD"])
    if checked_out_sha.casefold() != resolved_sha.casefold():
        raise SourceDriftError(
            f"Git checkout drifted from {resolved_sha} to {checked_out_sha}."
        )
    return repo_dir


def _safe_extract_zip(
    zip_file: zipfile.ZipFile,
    dest_dir: str,
    archive_roots: tuple[str, ...],
) -> None:
    dest_root = os.path.realpath(dest_dir)
    selected_members: list[zipfile.ZipInfo] = []
    for info in zip_file.infolist():
        archive_path = info.filename.rstrip("/")
        if not any(
            archive_path == root or archive_path.startswith(root + "/")
            for root in archive_roots
        ):
            continue
        if stat.S_ISLNK(info.external_attr >> 16):
            raise DownloadError("Archive contains symbolic links.")
        extracted_path = os.path.realpath(os.path.join(dest_dir, info.filename))
        if extracted_path == dest_root or extracted_path.startswith(dest_root + os.sep):
            selected_members.append(info)
            continue
        raise DownloadError("Archive contains files outside the destination.")
    zip_file.extractall(dest_dir, members=selected_members)


def _validate_relative_path(path: str) -> None:
    if os.path.isabs(path) or os.path.normpath(path).startswith(".."):
        raise InstallError("Skill path must be a relative path inside the repo.")


def _validate_skill_name(name: str) -> None:
    altsep = os.path.altsep
    if not name or os.path.sep in name or (altsep and altsep in name):
        raise InstallError("Skill name must be a single path segment.")
    if name in (".", ".."):
        raise InstallError("Invalid skill name.")


def _validate_skill(path: str, repo_root: str) -> None:
    repo_root_real = os.path.realpath(repo_root)
    skill_real = os.path.realpath(path)
    try:
        contained = os.path.commonpath((repo_root_real, skill_real)) == repo_root_real
    except ValueError:
        contained = False
    if not contained:
        raise InstallError(f"Skill path resolves outside the repository: {path}")
    relative_path = os.path.relpath(os.path.abspath(path), os.path.abspath(repo_root))
    current_path = os.path.abspath(repo_root)
    for path_part in _path_parts(relative_path):
        current_path = os.path.join(current_path, path_part)
        if os.path.islink(current_path):
            raise InstallError("Skill directories cannot contain symbolic links.")
    if not os.path.isdir(path):
        raise InstallError(f"Skill path not found: {path}")
    skill_md = os.path.join(path, "SKILL.md")
    if not os.path.isfile(skill_md):
        raise InstallError("SKILL.md not found in selected skill directory.")
    for current_root, directories, filenames in os.walk(path, followlinks=False):
        for name in (*directories, *filenames):
            if os.path.islink(os.path.join(current_root, name)):
                raise InstallError("Skill directories cannot contain symbolic links.")


def _copy_skill(src: str, dest_dir: str) -> None:
    os.makedirs(os.path.dirname(dest_dir), exist_ok=True)
    if os.path.exists(dest_dir):
        raise InstallError(f"Destination already exists: {dest_dir}")
    shutil.copytree(src, dest_dir, ignore=shutil.ignore_patterns(".git"))


def _build_repo_url(repository: RepositoryIdentity) -> str:
    return f"https://github.com/{repository.full_name}.git"


def _build_repo_ssh(repository: RepositoryIdentity) -> str:
    return f"git@github.com:{repository.full_name}.git"


def _prepare_repo(source: ResolvedSource, method: str, tmp_dir: str) -> str:
    if method in ("download", "auto"):
        try:
            return _download_repo_zip(
                source.repository, source.resolved_sha, source.paths, tmp_dir
            )
        except DownloadError as exc:
            if method == "download" or exc.status_code not in (401, 403, 404):
                raise
    if method in ("git", "auto"):
        last_error: InstallError | None = None
        for repo_url in (
            _build_repo_url(source.repository),
            _build_repo_ssh(source.repository),
        ):
            shutil.rmtree(os.path.join(tmp_dir, "repo"), ignore_errors=True)
            try:
                return _git_sparse_checkout(
                    repo_url, source.resolved_sha, source.paths, tmp_dir
                )
            except SourceDriftError:
                raise
            except InstallError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
    raise InstallError("Unsupported method.")


def _default_dest() -> str:
    return os.path.join(_runtime_home(), "skills")


def _path_parts(path: str) -> tuple[str, ...]:
    if path in ("", "."):
        return ()
    return tuple(part for part in path.split(os.sep) if part not in ("", "."))


def _display_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)[1:-1]


def _display_path(path: str) -> str:
    absolute_path = os.path.abspath(path)
    home = os.path.abspath(os.path.expanduser("~"))
    try:
        if os.path.commonpath((home, absolute_path)) == home:
            relative_path = os.path.relpath(absolute_path, home)
            path = "~" if relative_path == "." else os.path.join("~", relative_path)
    except ValueError:
        pass
    return _display_value(path)


def _parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description="Install a skill from GitHub.")
    parser.add_argument("--repo", help="owner/repo")
    parser.add_argument("--url", help="https://github.com/owner/repo[/tree/ref/path]")
    parser.add_argument(
        "--path",
        nargs="+",
        help="Path(s) to skill(s) inside repo",
    )
    parser.add_argument(
        "--ref",
        default=DEFAULT_REF,
        help="Branch, tag, explicit refs/... name, or full 40-character commit SHA",
    )
    parser.add_argument("--dest", help="Destination skills directory")
    parser.add_argument(
        "--name", help="Destination skill name (defaults to basename of path)"
    )
    parser.add_argument(
        "--method",
        choices=["auto", "download", "git"],
        default="auto",
    )
    return parser.parse_args(argv, namespace=Args())


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        source = _resolve_source(_resolve_source_request(args))
        for path in source.paths:
            _validate_relative_path(path)
        dest_root = args.dest or _default_dest()
        tmp_dir = tempfile.mkdtemp(prefix="skill-install-", dir=_tmp_root())
        try:
            repo_root = _prepare_repo(source, args.method, tmp_dir)
            _verify_repository_identity(source.repository)
            install_plan: list[tuple[str, str, str]] = []
            seen_destinations: set[str] = set()
            for path in source.paths:
                skill_name = args.name if len(source.paths) == 1 else None
                skill_name = skill_name or os.path.basename(path.rstrip("/"))
                _validate_skill_name(skill_name)
                dest_dir = os.path.join(dest_root, skill_name)
                destination_key = os.path.normpath(dest_dir).casefold()
                if destination_key in seen_destinations or os.path.exists(dest_dir):
                    raise InstallError(f"Destination already exists: {dest_dir}")
                skill_src = os.path.join(repo_root, path)
                _validate_skill(skill_src, repo_root)
                install_plan.append((path, skill_src, dest_dir))
                seen_destinations.add(destination_key)
            for _, skill_src, dest_dir in install_plan:
                _copy_skill(skill_src, dest_dir)
        finally:
            if os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
        print(f"Repository: {_display_value(source.repository.full_name)}")
        print(f"Requested ref: {_display_value(source.requested_ref)}")
        print(f"Resolved SHA: {source.resolved_sha}")
        print("Installed:")
        for path, _, dest_dir in install_plan:
            print(f"- {_display_value(path)} -> {_display_path(dest_dir)}")
        return 0
    except InstallError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
