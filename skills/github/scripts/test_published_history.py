# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from test_landed_worktrees import Fixture


def git(*args: str, cwd: Path | None = None, input_text: str | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, input=input_text, capture_output=True, text=True,
                          errors="surrogateescape", check=True).stdout.strip()


class PublicationTests(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.env = dict(os.environ)

    def create(self) -> Path:
        worktree = self.worktree("task-1")
        git("config", "user.name", "Fixture", cwd=worktree)
        git("config", "user.email", "fixture@example.invalid", cwd=worktree)
        (worktree / "change.txt").write_text("change\n")
        git("add", "change.txt", cwd=worktree)
        git("commit", "-q", "-m", "change", cwd=worktree)
        return worktree

    def read_publication(self, worktree: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(Path(__file__).with_name("published_history.py")), str(worktree)],
            env=self.env,
            capture_output=True,
            text=True,
        )

    def add_upstream(self, name: str = "upstream.git") -> Path:
        upstream = self.base / name
        git("init", "--bare", "-q", "-b", "main", str(upstream))
        git("remote", "add", "upstream", str(upstream), cwd=self.repo)
        return upstream

    def add_fork(self) -> Path:
        fork = self.base / "fork.git"
        git("init", "--bare", "-q", "-b", "main", str(fork))
        git("remote", "add", "fork", str(fork), cwd=self.repo)
        return fork

    def test_environment_cannot_redirect_proof_to_a_published_repository(self) -> None:
        worktree = self.create()
        self.env.update(GIT_DIR=str(self.repo / ".git"), GIT_WORK_TREE=str(self.repo))
        self.assertNotEqual(self.read_publication(worktree).returncode, 0)

    def test_remote_paths_and_refs_preserve_non_utf8_bytes(self) -> None:
        worktree = self.create()
        remote = self.base / os.fsdecode(b"remote-\xff.git")
        git("init", "--bare", str(remote))
        git("remote", "set-url", "origin", str(remote), cwd=worktree)
        git("push", "origin", "HEAD:refs/heads/" + os.fsdecode(b"branch-\xff"), cwd=worktree)
        result = self.read_publication(worktree)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_accepts_a_tip_published_only_on_its_branch_remote(self) -> None:
        self.add_fork()
        worktree = self.create()
        git("push", "-q", "-u", "fork", "HEAD:refs/heads/tested", cwd=worktree)
        completed = self.read_publication(worktree)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_keeps_a_tip_on_an_unrelated_remote(self) -> None:
        self.add_fork()
        worktree = self.create()
        git("push", "-q", "fork", "HEAD:refs/heads/tested", cwd=worktree)
        completed = self.read_publication(worktree)
        self.assertNotEqual(completed.returncode, 0)

    def test_accepts_an_upstream_only_published_tip(self) -> None:
        self.add_upstream()
        worktree = self.create()
        git("push", "-q", "upstream", "HEAD:refs/heads/tested", cwd=worktree)
        git("remote", "remove", "origin", cwd=worktree)
        completed = self.read_publication(worktree)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_accepts_an_ancestor_of_a_live_upstream_tip(self) -> None:
        self.add_upstream()
        worktree = self.create()
        tested_head = git("rev-parse", "HEAD", cwd=worktree)
        git("commit", "-q", "--allow-empty", "-m", "later upstream commit", cwd=worktree)
        git("push", "-q", "upstream", "HEAD:refs/heads/tested", cwd=worktree)
        git("reset", "--hard", tested_head, cwd=worktree)
        completed = self.read_publication(worktree)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_accepts_a_tip_on_an_independent_file_url_remote(self) -> None:
        upstream = self.add_upstream("upstream repo.git")
        worktree = self.create()
        git("remote", "set-url", "upstream", upstream.as_uri(), cwd=worktree)
        git("push", "-q", "upstream", "HEAD:refs/heads/tested", cwd=worktree)
        completed = self.read_publication(worktree)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_keeps_unpublished_work_with_upstream_configured(self) -> None:
        self.add_upstream()
        git("push", "-q", "upstream", "main", cwd=self.repo)
        worktree = self.create()
        completed = self.read_publication(worktree)
        self.assertEqual(completed.returncode, 1)

    def test_keeps_work_when_only_a_stale_upstream_tracking_ref_remains(self) -> None:
        upstream = self.add_upstream()
        worktree = self.create()
        git("push", "-q", "upstream", "HEAD:refs/heads/tested", cwd=worktree)
        git("fetch", "-q", "upstream", cwd=worktree)
        git("update-ref", "-d", "refs/heads/tested", cwd=upstream)
        self.assertEqual(git("rev-parse", "upstream/tested", cwd=worktree), git("rev-parse", "HEAD", cwd=worktree))
        completed = self.read_publication(worktree)
        self.assertEqual(completed.returncode, 1)

    def test_keeps_work_when_upstream_cannot_be_resolved(self) -> None:
        self.add_upstream()
        worktree = self.create()
        git("remote", "set-url", "upstream", str(self.base / "missing.git"), cwd=worktree)
        completed = self.read_publication(worktree)
        self.assertEqual(completed.returncode, 1)

    def test_keeps_work_when_network_upstream_cannot_be_read(self) -> None:
        self.add_upstream()
        worktree = self.create()
        self.env["GIT_SSH_COMMAND"] = "false"
        for url in ("ssh://example.invalid/repo.git", "example.invalid:repo.git"):
            with self.subTest(url=url):
                git("remote", "set-url", "upstream", url, cwd=worktree)
                completed = self.read_publication(worktree)
                self.assertEqual(completed.returncode, 1)

    def test_origin_publication_does_not_require_upstream_to_be_available(self) -> None:
        self.add_upstream()
        worktree = self.create()
        git("push", "-q", "origin", "work/task-1", cwd=worktree)
        git("remote", "set-url", "upstream", str(self.base / "missing.git"), cwd=worktree)
        completed = self.read_publication(worktree)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_keeps_work_when_upstream_points_back_to_the_same_repository(self) -> None:
        worktree = self.create()
        alias = self.base / "repo-alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        colon_alias = self.base / "repo:alias"
        colon_alias.symlink_to(self.repo, target_is_directory=True)
        scheme_alias = self.base / "x:" / "alias"
        scheme_alias.parent.mkdir()
        scheme_alias.symlink_to(self.repo, target_is_directory=True)
        urls = (
            str(self.repo),
            str(alias),
            self.repo.as_uri(),
            self.repo.as_uri().replace("file://", "file:///%2e", 1),
            self.repo.as_uri().replace("file://", "file://somehost"),
            "~/" + os.path.relpath(self.repo, Path(self.env["HOME"])),
            str(colon_alias),
            os.path.relpath(colon_alias, worktree),
            os.path.relpath(scheme_alias, worktree).replace("x:/", "x://"),
            ".",
        )
        for url in urls:
            with self.subTest(url=url):
                git("config", "remote.upstream.url", url, cwd=worktree)
                completed = self.read_publication(worktree)
                self.assertEqual(completed.returncode, 1)

    def test_keeps_work_when_origin_points_back_to_the_same_repository(self) -> None:
        worktree = self.create()
        git("remote", "set-url", "origin", str(self.repo), cwd=worktree)
        completed = self.read_publication(worktree)
        self.assertEqual(completed.returncode, 1)

    def test_keeps_work_when_a_local_remote_borrows_its_objects(self) -> None:
        worktree = self.create()
        upstream = self.base / "shared-clone.git"
        git("clone", "-q", "--shared", "--bare", str(self.repo), str(upstream))
        git("remote", "add", "upstream", str(upstream), cwd=worktree)
        completed = self.read_publication(worktree)
        self.assertEqual(completed.returncode, 1)

    def test_keeps_an_ancestor_missing_from_a_shallow_local_remote(self) -> None:
        worktree = self.create()
        tested_head = git("rev-parse", "HEAD", cwd=worktree)
        git("commit", "-q", "--allow-empty", "-m", "later commit", cwd=worktree)
        upstream = self.base / "shallow.git"
        git("clone", "-q", "--bare", "--depth", "1", worktree.as_uri(), str(upstream))
        git("reset", "--hard", tested_head, cwd=worktree)
        git("remote", "add", "upstream", str(upstream), cwd=worktree)
        self.assertEqual(git("rev-parse", "--is-shallow-repository", cwd=upstream), "true")
        completed = self.read_publication(worktree)
        self.assertEqual(completed.returncode, 1)

    def test_keeps_work_when_a_local_remote_relies_on_promised_objects(self) -> None:
        upstream = self.add_upstream()
        worktree = self.create()
        git("push", "-q", "upstream", "HEAD:refs/heads/tested", cwd=worktree)
        git("config", "remote.source.promisor", "true", cwd=upstream)
        completed = self.read_publication(worktree)
        self.assertEqual(completed.returncode, 1)

    def test_keeps_work_when_git_transport_and_discovery_resolve_different_repositories(self) -> None:
        worktree = self.create()
        unrelated = self.base / "unrelated"
        git("init", "-q", "-b", "main", str(unrelated))
        child = unrelated / "child"
        child.mkdir()
        child.with_suffix(".git").symlink_to(self.repo / ".git", target_is_directory=True)
        git("remote", "add", "upstream", str(child), cwd=worktree)
        completed = self.read_publication(worktree)
        self.assertEqual(completed.returncode, 1)

    def test_local_replace_refs_cannot_prove_publication(self) -> None:
        worktree = self.create()
        tested_head = git("rev-parse", "HEAD", cwd=worktree)
        fake_descendant = git(
            "commit-tree", git("rev-parse", "HEAD^{tree}", cwd=worktree), "-p", tested_head, "-m", "fake descendant", cwd=worktree
        )
        git("replace", git("rev-parse", "origin/main", cwd=worktree), fake_descendant, cwd=worktree)
        completed = self.read_publication(worktree)
        self.assertEqual(completed.returncode, 1)

    def test_legacy_grafts_cannot_prove_publication(self) -> None:
        worktree = self.create()
        published = git("rev-parse", "origin/main", cwd=worktree)
        head = git("rev-parse", "HEAD", cwd=worktree)
        (self.repo / ".git" / "info" / "grafts").write_text(f"{published} {head}\n")
        completed = self.read_publication(worktree)
        self.assertEqual(completed.returncode, 1)
        git("replace", "--convert-graft-file", cwd=worktree)
        completed = self.read_publication(worktree)
        self.assertEqual(completed.returncode, 1)

    def test_global_promisor_settings_do_not_reject_an_independent_remote(self) -> None:
        self.add_upstream()
        worktree = self.create()
        git("push", "-q", "upstream", "HEAD:refs/heads/tested", cwd=worktree)
        global_config = self.base / "global.gitconfig"
        global_config.write_text('[remote "elsewhere"]\n    promisor = true\n')
        self.env["GIT_CONFIG_GLOBAL"] = str(global_config)
        completed = self.read_publication(worktree)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_external_grafts_cannot_prove_publication(self) -> None:
        worktree = self.create()
        published = git("rev-parse", "origin/main", cwd=worktree)
        head = git("rev-parse", "HEAD", cwd=worktree)
        grafts = self.base / "external.grafts"
        grafts.write_text(f"{published} {head}\n")
        for path in (str(grafts), os.path.relpath(grafts, worktree)):
            with self.subTest(path=path):
                self.env["GIT_GRAFT_FILE"] = path
                completed = self.read_publication(worktree)
                self.assertEqual(completed.returncode, 1)

    def test_original_history_recovery_preserves_grafts_and_retires_published_work(self) -> None:
        worktree = self.create()
        git("push", "-q", "origin", "work/task-1", cwd=worktree)
        published = git("rev-parse", "origin/main", cwd=worktree)
        head = git("rev-parse", "HEAD", cwd=worktree)
        grafts = self.repo / ".git" / "info" / "grafts"
        contents = f"{published} {head}\n"
        grafts.write_text(contents)
        completed = self.read_publication(worktree)
        self.assertEqual(completed.returncode, 1)
        self.env["GIT_GRAFT_FILE"] = "/dev/null"
        completed = self.read_publication(worktree)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(grafts.read_text(), contents)


if __name__ == "__main__":
    unittest.main()
