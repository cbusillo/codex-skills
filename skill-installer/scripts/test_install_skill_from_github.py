#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused offline tests for immutable GitHub skill installs."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import urllib.error
from unittest.mock import patch
import zipfile


SCRIPT_PATH = Path(__file__).with_name("install-skill-from-github.py")
SPEC = importlib.util.spec_from_file_location("install_skill_from_github", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

REPOSITORY = "octo/example"
REPOSITORY_ID = 1234
RESOLVED_SHA = "1" * 40
OTHER_SHA = "2" * 40
TAG_SHA = "3" * 40


def response(data: object) -> bytes:
    return json.dumps(data).encode("utf-8")


def http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "test error", {}, None)


def repository_payload(
    *, full_name: str = REPOSITORY, repository_id: int = REPOSITORY_ID
) -> dict[str, object]:
    return {
        "id": repository_id,
        "full_name": full_name,
        "url": f"https://api.github.com/repos/{full_name}",
    }


def ref_payload(
    qualified_ref: str, object_type: str, sha: str
) -> dict[str, object]:
    return {
        "ref": qualified_ref,
        "url": f"https://api.github.com/repos/{REPOSITORY}/git/refs/"
        f"{qualified_ref.removeprefix('refs/')}",
        "object": {"type": object_type, "sha": sha},
    }


def commit_payload(sha: str) -> dict[str, object]:
    return {
        "sha": sha,
        "url": f"https://api.github.com/repos/{REPOSITORY}/git/commits/{sha}",
    }


def source(ref: str) -> object:
    return MODULE.Source(
        owner="octo",
        repo="example",
        ref=ref,
        paths=("skills/demo",),
    )


def test_mutable_branch_resolves_to_one_commit() -> None:
    calls: list[str] = []

    def fake_request(url: str) -> bytes:
        calls.append(url)
        if url == "https://api.github.com/repos/octo/example":
            return response(repository_payload())
        if url.endswith("/git/ref/heads/main"):
            return response(ref_payload("refs/heads/main", "commit", RESOLVED_SHA))
        if url.endswith("/git/ref/tags/main"):
            raise http_error(url, 404)
        if url.endswith(f"/git/commits/{RESOLVED_SHA}"):
            return response(commit_payload(RESOLVED_SHA))
        raise AssertionError(url)

    with patch.object(MODULE, "_request", side_effect=fake_request):
        resolved = MODULE._resolve_source(source("main"))

    assert resolved.requested_ref == "main"
    assert resolved.resolved_sha == RESOLVED_SHA
    assert any("/git/ref/heads/main" in url for url in calls)
    assert all("codeload.github.com" not in url for url in calls)


def test_full_sha_is_validated_without_named_ref_lookup() -> None:
    calls: list[str] = []

    def fake_request(url: str) -> bytes:
        calls.append(url)
        if url == "https://api.github.com/repos/octo/example":
            return response(repository_payload())
        if url.endswith(f"/git/commits/{RESOLVED_SHA}"):
            return response(commit_payload(RESOLVED_SHA))
        raise AssertionError(url)

    with patch.object(MODULE, "_request", side_effect=fake_request):
        resolved = MODULE._resolve_source(source(RESOLVED_SHA))

    assert resolved.resolved_sha == RESOLVED_SHA
    assert not any("/git/ref/" in url for url in calls)


def test_bare_branch_and_tag_collision_is_rejected() -> None:
    def fake_request(url: str) -> bytes:
        if url == "https://api.github.com/repos/octo/example":
            return response(repository_payload())
        if url.endswith("/git/ref/heads/release"):
            return response(ref_payload("refs/heads/release", "commit", RESOLVED_SHA))
        if url.endswith("/git/ref/tags/release"):
            return response(ref_payload("refs/tags/release", "commit", OTHER_SHA))
        raise AssertionError(url)

    with patch.object(MODULE, "_request", side_effect=fake_request):
        try:
            MODULE._resolve_source(source("release"))
        except MODULE.InstallError as exc:
            assert "Ambiguous ref" in str(exc)
        else:
            raise AssertionError("Expected ambiguous ref rejection")


def test_annotated_tag_is_peeled_to_commit() -> None:
    def fake_request(url: str) -> bytes:
        if url == "https://api.github.com/repos/octo/example":
            return response(repository_payload())
        if url.endswith("/git/ref/tags/v1"):
            return response(ref_payload("refs/tags/v1", "tag", TAG_SHA))
        if url.endswith(f"/git/tags/{TAG_SHA}"):
            return response(
                {
                    "url": f"https://api.github.com/repos/{REPOSITORY}/git/tags/{TAG_SHA}",
                    "object": {"type": "commit", "sha": RESOLVED_SHA},
                }
            )
        if url.endswith(f"/git/commits/{RESOLVED_SHA}"):
            return response(commit_payload(RESOLVED_SHA))
        raise AssertionError(url)

    with patch.object(MODULE, "_request", side_effect=fake_request):
        resolved = MODULE._resolve_source(source("refs/tags/v1"))

    assert resolved.resolved_sha == RESOLVED_SHA


def test_short_sha_is_rejected_after_exact_ref_checks() -> None:
    short_sha = RESOLVED_SHA[:12]

    def fake_request(url: str) -> bytes:
        if url.endswith(f"/git/ref/heads/{short_sha}") or url.endswith(
            f"/git/ref/tags/{short_sha}"
        ):
            raise http_error(url, 404)
        raise AssertionError(url)

    repository = MODULE.RepositoryIdentity(REPOSITORY, REPOSITORY_ID)
    with patch.object(MODULE, "_request", side_effect=fake_request):
        try:
            MODULE._resolve_requested_ref(repository, short_sha)
        except MODULE.InstallError as exc:
            assert "full 40-character SHA" in str(exc)
        else:
            raise AssertionError("Expected short SHA rejection")


def test_download_to_git_fallback_reuses_resolved_sha() -> None:
    resolved = MODULE.ResolvedSource(
        repository=MODULE.RepositoryIdentity(REPOSITORY, REPOSITORY_ID),
        requested_ref="main",
        resolved_sha=RESOLVED_SHA,
        paths=("skills/demo",),
    )
    observed: list[tuple[str, str]] = []

    def fake_download(
        repository: object,
        sha: str,
        paths: tuple[str, ...],
        dest_dir: str,
    ) -> str:
        observed.append(("download", sha))
        raise MODULE.DownloadError("Download failed: HTTP 404", status_code=404)

    def fake_git(repo_url: str, sha: str, paths: tuple[str, ...], dest_dir: str) -> str:
        observed.append(("git", sha))
        return os.path.join(dest_dir, "repo")

    with tempfile.TemporaryDirectory() as temporary_directory:
        with (
            patch.object(MODULE, "_download_repo_zip", side_effect=fake_download),
            patch.object(MODULE, "_git_sparse_checkout", side_effect=fake_git),
        ):
            MODULE._prepare_repo(resolved, "auto", temporary_directory)

    assert observed == [("download", RESOLVED_SHA), ("git", RESOLVED_SHA)]


def test_repository_identity_drift_is_rejected() -> None:
    def fake_request(url: str) -> bytes:
        return response(repository_payload(repository_id=REPOSITORY_ID + 1))

    repository = MODULE.RepositoryIdentity(REPOSITORY, REPOSITORY_ID)
    with patch.object(MODULE, "_request", side_effect=fake_request):
        try:
            MODULE._verify_repository_identity(repository)
        except MODULE.InstallError as exc:
            assert "identity changed" in str(exc)
        else:
            raise AssertionError("Expected repository identity drift rejection")


def test_git_checkout_head_must_match_resolved_sha() -> None:
    commands: list[list[str]] = []

    def fake_run_git(args: list[str]) -> str:
        commands.append(args)
        if args[-2:] == ["rev-parse", "HEAD"]:
            return OTHER_SHA
        return ""

    with tempfile.TemporaryDirectory() as temporary_directory:
        with patch.object(MODULE, "_run_git", side_effect=fake_run_git):
            try:
                MODULE._git_sparse_checkout(
                    "https://github.com/octo/example.git",
                    RESOLVED_SHA,
                    ("skills/demo",),
                    temporary_directory,
                )
            except MODULE.InstallError as exc:
                assert "checkout drifted" in str(exc)
            else:
                raise AssertionError("Expected checkout drift rejection")

    fetch_command = next(command for command in commands if "fetch" in command)
    assert fetch_command[-1] == RESOLVED_SHA
    sparse_command = next(command for command in commands if "sparse-checkout" in command and "set" in command)
    assert sparse_command[-2:] == ["--", "skills/demo"]


def test_slash_ref_url_is_resolved_without_guessing() -> None:
    repository = MODULE.RepositoryIdentity(REPOSITORY, REPOSITORY_ID)
    request = MODULE.Source(
        owner="octo",
        repo="example",
        ref=None,
        paths=(),
        url_ref_path=("feature", "topic", "skills", "demo"),
        url_kind="tree",
    )

    def fake_resolve_ref(repo: object, requested_ref: str) -> str:
        if requested_ref == "feature/topic":
            return RESOLVED_SHA
        raise MODULE.RefNotFoundError(requested_ref)

    with (
        patch.object(MODULE, "_resolve_repository_identity", return_value=repository),
        patch.object(MODULE, "_resolve_requested_ref", side_effect=fake_resolve_ref),
        patch.object(
            MODULE,
            "_path_type_at_commit",
            side_effect=lambda repo, path, sha: "dir" if path == "skills/demo" else None,
        ),
        patch.object(MODULE, "_verify_repository_identity"),
    ):
        resolved = MODULE._resolve_source(request)

    assert resolved.requested_ref == "feature/topic"
    assert resolved.paths == ("skills/demo",)


def test_url_path_override_preserves_the_url_ref() -> None:
    repository = MODULE.RepositoryIdentity(REPOSITORY, REPOSITORY_ID)
    request = MODULE._parse_github_url(
        "https://github.com/octo/example/tree/main/old/path",
        "main",
        ["skills/demo"],
    )

    def fake_resolve_ref(repo: object, requested_ref: str) -> str:
        if requested_ref == "main":
            return RESOLVED_SHA
        raise MODULE.RefNotFoundError(requested_ref)

    with (
        patch.object(MODULE, "_resolve_repository_identity", return_value=repository),
        patch.object(MODULE, "_resolve_requested_ref", side_effect=fake_resolve_ref),
        patch.object(MODULE, "_verify_repository_identity"),
    ):
        resolved = MODULE._resolve_source(request)

    assert resolved.requested_ref == "main"
    assert resolved.paths == ("skills/demo",)


def test_hex_leading_slash_ref_is_not_mistaken_for_short_sha() -> None:
    repository = MODULE.RepositoryIdentity(REPOSITORY, REPOSITORY_ID)
    request = MODULE.Source(
        owner="octo",
        repo="example",
        ref=None,
        paths=(),
        url_ref_path=("deadbeef", "topic", "skills", "demo"),
        url_kind="tree",
    )

    def fake_resolve_ref(repo: object, requested_ref: str) -> str:
        if requested_ref == "deadbeef":
            raise MODULE.ShortShaError(requested_ref)
        if requested_ref == "deadbeef/topic":
            return RESOLVED_SHA
        raise MODULE.RefNotFoundError(requested_ref)

    with (
        patch.object(MODULE, "_resolve_repository_identity", return_value=repository),
        patch.object(MODULE, "_resolve_requested_ref", side_effect=fake_resolve_ref),
        patch.object(
            MODULE,
            "_path_type_at_commit",
            side_effect=lambda repo, path, sha: "dir" if path == "skills/demo" else None,
        ),
        patch.object(MODULE, "_verify_repository_identity"),
    ):
        resolved = MODULE._resolve_source(request)

    assert resolved.requested_ref == "deadbeef/topic"


def test_git_checkout_drift_does_not_retry_another_transport() -> None:
    resolved = MODULE.ResolvedSource(
        repository=MODULE.RepositoryIdentity(REPOSITORY, REPOSITORY_ID),
        requested_ref="main",
        resolved_sha=RESOLVED_SHA,
        paths=("skills/demo",),
    )
    attempts: list[str] = []

    def fake_git(repo_url: str, sha: str, paths: tuple[str, ...], dest_dir: str) -> str:
        attempts.append(repo_url)
        raise MODULE.SourceDriftError("drift")

    with tempfile.TemporaryDirectory() as temporary_directory:
        with patch.object(MODULE, "_git_sparse_checkout", side_effect=fake_git):
            try:
                MODULE._prepare_repo(resolved, "git", temporary_directory)
            except MODULE.SourceDriftError:
                pass
            else:
                raise AssertionError("Expected source drift rejection")

    assert attempts == ["https://github.com/octo/example.git"]


def test_skill_symlinks_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        skill = root / "skills/demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
        (skill / "outside").symlink_to(root)
        try:
            MODULE._validate_skill(str(skill), str(root))
        except MODULE.InstallError as exc:
            assert "symbolic links" in str(exc)
        else:
            raise AssertionError("Expected skill symlink rejection")


def test_archive_symlinks_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        archive_path = Path(temporary_directory) / "repo.zip"
        link_info = zipfile.ZipInfo("example-main/skills/demo/outside")
        link_info.create_system = 3
        link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(link_info, "/etc/passwd")
        with zipfile.ZipFile(archive_path) as archive:
            try:
                MODULE._safe_extract_zip(
                    archive,
                    temporary_directory,
                    ("example-main/skills/demo",),
                )
            except MODULE.DownloadError as exc:
                assert "symbolic links" in str(exc)
            else:
                raise AssertionError("Expected archive symlink rejection")


def test_unrelated_archive_symlinks_do_not_block_selected_skill() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        archive_path = Path(temporary_directory) / "repo.zip"
        link_info = zipfile.ZipInfo("example-main/docs/outside")
        link_info.create_system = 3
        link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(link_info, "/etc/passwd")
            archive.writestr(
                "example-main/skills/demo/SKILL.md",
                "---\nname: demo\n---\n",
            )
        with zipfile.ZipFile(archive_path) as archive:
            MODULE._safe_extract_zip(
                archive,
                temporary_directory,
                ("example-main/skills/demo",),
            )
        assert Path(
            temporary_directory,
            "example-main/skills/demo/SKILL.md",
        ).is_file()
        assert not Path(temporary_directory, "example-main/docs/outside").exists()


def test_copy_ignores_git_metadata() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        source_root = root / "source"
        destination = root / "destination"
        (source_root / ".git").mkdir(parents=True)
        (source_root / ".git/config").write_text("secret", encoding="utf-8")
        (source_root / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
        MODULE._copy_skill(str(source_root), str(destination))
        assert (destination / "SKILL.md").is_file()
        assert not (destination / ".git").exists()


def test_provenance_output_escapes_control_characters() -> None:
    assert MODULE._display_value("feature\nname") == "feature\\nname"


def test_unsafe_url_path_is_rejected_before_github_request() -> None:
    with patch.object(
        MODULE,
        "_request",
        side_effect=AssertionError("GitHub request must not run"),
    ):
        try:
            MODULE._parse_github_url(
                "https://github.com/octo/example/tree/main/%2e%2e/private",
                "main",
                None,
            )
        except MODULE.InstallError as exc:
            assert "unsafe path segment" in str(exc)
        else:
            raise AssertionError("Expected unsafe URL rejection")


def test_case_colliding_destinations_are_rejected_before_copy() -> None:
    resolved = MODULE.ResolvedSource(
        repository=MODULE.RepositoryIdentity(REPOSITORY, REPOSITORY_ID),
        requested_ref="main",
        resolved_sha=RESOLVED_SHA,
        paths=("skills/Demo", "examples/demo"),
    )
    stdout = StringIO()
    stderr = StringIO()

    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        repo_root = root / "source"
        for relative_path in resolved.paths:
            skill_root = repo_root / relative_path
            skill_root.mkdir(parents=True)
            (skill_root / "SKILL.md").write_text(
                "---\nname: demo\n---\n",
                encoding="utf-8",
            )
        dest_root = root / "dest"
        with (
            patch.object(MODULE, "_resolve_source", return_value=resolved),
            patch.object(MODULE, "_prepare_repo", return_value=str(repo_root)),
            patch.object(MODULE, "_verify_repository_identity"),
            patch.object(MODULE, "_tmp_root", return_value=temporary_directory),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = MODULE.main(
                [
                    "--repo",
                    REPOSITORY,
                    "--path",
                    *resolved.paths,
                    "--dest",
                    str(dest_root),
                ]
            )

        assert not dest_root.exists()

    assert exit_code == 1
    assert "Destination already exists" in stderr.getvalue()


def test_success_output_reports_public_safe_provenance() -> None:
    resolved = MODULE.ResolvedSource(
        repository=MODULE.RepositoryIdentity(REPOSITORY, REPOSITORY_ID),
        requested_ref="main",
        resolved_sha=RESOLVED_SHA,
        paths=("skills/demo",),
    )
    stdout = StringIO()
    stderr = StringIO()

    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        repo_root = root / "source"
        skill_root = repo_root / "skills/demo"
        skill_root.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
        dest_root = root / "dest"
        with (
            patch.object(MODULE, "_resolve_source", return_value=resolved),
            patch.object(MODULE, "_prepare_repo", return_value=str(repo_root)),
            patch.object(MODULE, "_verify_repository_identity"),
            patch.object(MODULE, "_tmp_root", return_value=temporary_directory),
            patch.dict(os.environ, {"GITHUB_TOKEN": "never-print-this-token"}),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = MODULE.main(
                [
                    "--repo",
                    REPOSITORY,
                    "--path",
                    "skills/demo",
                    "--dest",
                    str(dest_root),
                ]
            )

        assert (dest_root / "demo/SKILL.md").is_file()

    output = stdout.getvalue()
    assert exit_code == 0, stderr.getvalue()
    assert f"Repository: {REPOSITORY}" in output
    assert "Requested ref: main" in output
    assert f"Resolved SHA: {RESOLVED_SHA}" in output
    assert "- skills/demo -> " in output
    assert "never-print-this-token" not in output


TESTS = [
    test_mutable_branch_resolves_to_one_commit,
    test_full_sha_is_validated_without_named_ref_lookup,
    test_bare_branch_and_tag_collision_is_rejected,
    test_annotated_tag_is_peeled_to_commit,
    test_short_sha_is_rejected_after_exact_ref_checks,
    test_download_to_git_fallback_reuses_resolved_sha,
    test_repository_identity_drift_is_rejected,
    test_git_checkout_head_must_match_resolved_sha,
    test_slash_ref_url_is_resolved_without_guessing,
    test_url_path_override_preserves_the_url_ref,
    test_hex_leading_slash_ref_is_not_mistaken_for_short_sha,
    test_git_checkout_drift_does_not_retry_another_transport,
    test_skill_symlinks_are_rejected,
    test_archive_symlinks_are_rejected,
    test_unrelated_archive_symlinks_do_not_block_selected_skill,
    test_copy_ignores_git_metadata,
    test_provenance_output_escapes_control_characters,
    test_unsafe_url_path_is_rejected_before_github_request,
    test_case_colliding_destinations_are_rejected_before_copy,
    test_success_output_reports_public_safe_provenance,
]


def main() -> int:
    for test in TESTS:
        test()
    print(f"skill installer tests passed ({len(TESTS)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
