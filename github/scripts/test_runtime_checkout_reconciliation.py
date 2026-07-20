#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
# ]
# ///
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


SCRIPT = Path(__file__).with_name("reconcile-runtime-checkout.py")


@dataclass
class RuntimeFixture:
    repo: str
    remote: Path
    runtime: Path
    merged: Path
    landing: Path
    code_home: Path
    initial_sha: str
    head_sha: str
    landing_sha: str

    @property
    def script(self) -> Path:
        return self.merged / "github" / "scripts" / SCRIPT.name

    def run(
        self,
        landing_sha: str | None = None,
        *,
        code_home: Path | None = None,
        extra_env: dict[str, str] | None = None,
        repo: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        env = os.environ.copy()
        env["CODE_HOME"] = str(code_home or self.code_home)
        env["CODEX_HOME"] = str(self.code_home.parent / "ignored-codex-home")
        env.update(extra_env or {})
        proc = subprocess.run(
            [
                sys.executable,
                str(self.script),
                "--merged-worktree",
                str(self.merged),
                "--repo",
                repo or self.repo,
                "--landing-sha",
                landing_sha or self.landing_sha,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        return proc, json.loads(proc.stdout)


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def configure_git(repo: Path) -> None:
    git(repo, "config", "user.name", "Runtime Test")
    git(repo, "config", "user.email", "runtime-test@example.invalid")


def commit_file(repo: Path, relative_path: str, content: str, message: str) -> str:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    git(repo, "add", relative_path)
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def build_runtime_fixture(tmp_path: Path, *, helper_in_initial: bool = True) -> RuntimeFixture:
    repo = "example/repo"
    remote = tmp_path / "example" / "repo.git"
    seed = tmp_path / "seed"
    runtime = tmp_path / "runtime"
    merged = tmp_path / "merged"
    landing = tmp_path / "landing"
    code_home = tmp_path / "code-home"

    remote.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(seed)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    configure_git(seed)
    metadata = seed / ".github" / "github.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text('{"defaultBranch":"main"}\n')
    (seed / ".gitignore").write_text("runtime-secret.txt\n")
    git(seed, "add", ".github/github.json", ".gitignore")
    if helper_in_initial:
        copied_script = seed / "github" / "scripts" / SCRIPT.name
        copied_script.parent.mkdir(parents=True)
        shutil.copy2(SCRIPT, copied_script)
        git(seed, "add", f"github/scripts/{SCRIPT.name}")
    git(seed, "commit", "-m", "initial runtime helper")
    initial_sha = git(seed, "rev-parse", "HEAD")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-u", "origin", "main")

    subprocess.run(
        ["git", "clone", str(remote), str(runtime)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    configure_git(runtime)
    git(runtime, "worktree", "add", "-b", "runtime-test-source", str(merged), "main")
    if not helper_in_initial:
        copied_script = merged / "github" / "scripts" / SCRIPT.name
        copied_script.parent.mkdir(parents=True)
        shutil.copy2(SCRIPT, copied_script)
        git(merged, "add", f"github/scripts/{SCRIPT.name}")
    (merged / "feature.txt").write_text("feature\n")
    git(merged, "add", "feature.txt")
    git(merged, "commit", "-m", "feature change")
    head_sha = git(merged, "rev-parse", "HEAD")
    git(merged, "push", "-u", "origin", "runtime-test-source")

    subprocess.run(
        ["git", "clone", str(remote), str(landing)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    configure_git(landing)
    git(landing, "merge", "--no-ff", "origin/runtime-test-source", "-m", "merge feature")
    landing_sha = git(landing, "rev-parse", "HEAD")
    git(landing, "push", "origin", "main")

    code_home.mkdir()
    (code_home / "skills").symlink_to(runtime, target_is_directory=True)
    return RuntimeFixture(
        repo=repo,
        remote=remote,
        runtime=runtime,
        merged=merged,
        landing=landing,
        code_home=code_home,
        initial_sha=initial_sha,
        head_sha=head_sha,
        landing_sha=landing_sha,
    )


def test_reconcile_fast_forwards_symlinked_runtime_and_is_idempotent(tmp_path: Path) -> None:
    fixture = build_runtime_fixture(tmp_path)

    first_proc, first = fixture.run()

    assert first_proc.returncode == 0
    assert first["status"] == "synchronized"
    assert first["reason_code"] == "runtime_fast_forwarded"
    assert first["runtime_home_source"] == "CODE_HOME"
    assert first["binding"] == "shared_git_common_dir"
    assert first["helper_source_verified"] is True
    assert first["before_sha"] == fixture.initial_sha
    assert first["fetched_sha"] == fixture.landing_sha
    assert first["after_sha"] == fixture.landing_sha
    assert git(fixture.runtime, "rev-parse", "HEAD") == fixture.landing_sha

    second_proc, second = fixture.run()

    assert second_proc.returncode == 0
    assert second["status"] == "already_current"
    assert second["reason_code"] == "runtime_current"
    assert second["after_sha"] == fixture.landing_sha


def test_reconcile_bootstraps_when_runtime_does_not_have_helper(tmp_path: Path) -> None:
    fixture = build_runtime_fixture(tmp_path, helper_in_initial=False)
    runtime_helper = fixture.runtime / "github" / "scripts" / SCRIPT.name
    assert not runtime_helper.exists()

    proc, receipt = fixture.run()

    assert proc.returncode == 0
    assert receipt["status"] == "synchronized"
    assert receipt["helper_landing_verified"] is True
    assert receipt["helper_tip_verified"] is True
    assert runtime_helper.exists()


def test_reconcile_ignores_inherited_git_worktree_environment(tmp_path: Path) -> None:
    fixture = build_runtime_fixture(tmp_path)
    redirected_config = tmp_path / "redirected-gitconfig"
    subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(redirected_config),
            f"url.{tmp_path / 'wrong-remote.git'}.insteadOf",
            str(fixture.remote),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    proc, receipt = fixture.run(
        extra_env={
            "GIT_CONFIG_GLOBAL": str(redirected_config),
            "GIT_DIR": str(fixture.landing / ".git"),
            "GIT_WORK_TREE": str(fixture.landing),
            "GIT_INDEX_FILE": str(fixture.landing / ".git" / "index"),
        }
    )

    assert proc.returncode == 0
    assert receipt["status"] == "synchronized"
    assert git(fixture.runtime, "rev-parse", "HEAD") == fixture.landing_sha


def test_reconcile_blocks_dirty_runtime_without_fetching_or_mutating(tmp_path: Path) -> None:
    fixture = build_runtime_fixture(tmp_path)
    (fixture.runtime / "dirty.txt").write_text("dirty\n")

    proc, receipt = fixture.run()

    assert proc.returncode == 2
    assert receipt["status"] == "blocked"
    assert receipt["reason_code"] == "runtime_dirty"
    assert receipt["blockers"] == ["runtime_dirty"]
    assert receipt["fetched_sha"] is None
    assert git(fixture.runtime, "rev-parse", "HEAD") == fixture.initial_sha


def test_reconcile_blocks_assume_unchanged_runtime_file(tmp_path: Path) -> None:
    fixture = build_runtime_fixture(tmp_path)
    git(fixture.runtime, "update-index", "--assume-unchanged", ".gitignore")
    (fixture.runtime / ".gitignore").write_text("local hidden edit\n")
    assert git(fixture.runtime, "status", "--porcelain") == ""

    proc, receipt = fixture.run()

    assert proc.returncode == 2
    assert receipt["status"] == "blocked"
    assert receipt["reason_code"] == "runtime_hidden_index_state"
    assert (fixture.runtime / ".gitignore").read_text() == "local hidden edit\n"
    assert git(fixture.runtime, "rev-parse", "HEAD") == fixture.initial_sha


def test_reconcile_blocks_runtime_on_wrong_branch(tmp_path: Path) -> None:
    fixture = build_runtime_fixture(tmp_path)
    git(fixture.runtime, "switch", "-c", "runtime-work")

    proc, receipt = fixture.run()

    assert proc.returncode == 2
    assert receipt["status"] == "blocked"
    assert receipt["reason_code"] == "runtime_wrong_branch"
    assert git(fixture.runtime, "branch", "--show-current") == "runtime-work"


def test_reconcile_blocks_runtime_origin_repo_mismatch(tmp_path: Path) -> None:
    fixture = build_runtime_fixture(tmp_path)

    proc, receipt = fixture.run(repo="other/repo")

    assert proc.returncode == 2
    assert receipt["status"] == "blocked"
    assert receipt["reason_code"] == "runtime_origin_repo_mismatch"
    assert receipt["origin_repo"] == fixture.repo
    assert git(fixture.runtime, "rev-parse", "HEAD") == fixture.initial_sha


def test_reconcile_blocks_ahead_runtime(tmp_path: Path) -> None:
    fixture = build_runtime_fixture(tmp_path)
    first_proc, _ = fixture.run()
    assert first_proc.returncode == 0
    local_sha = commit_file(fixture.runtime, "local.txt", "local\n", "local runtime commit")

    proc, receipt = fixture.run()

    assert proc.returncode == 2
    assert receipt["status"] == "blocked"
    assert receipt["reason_code"] == "runtime_ahead"
    assert receipt["after_sha"] == local_sha
    assert git(fixture.runtime, "rev-parse", "HEAD") == local_sha


def test_reconcile_blocks_diverged_runtime(tmp_path: Path) -> None:
    fixture = build_runtime_fixture(tmp_path)
    local_sha = commit_file(fixture.runtime, "local.txt", "local\n", "diverged runtime commit")

    proc, receipt = fixture.run()

    assert proc.returncode == 2
    assert receipt["status"] == "blocked"
    assert receipt["reason_code"] == "runtime_diverged"
    assert receipt["after_sha"] == local_sha
    assert git(fixture.runtime, "rev-parse", "HEAD") == local_sha


def test_reconcile_rejects_landing_outside_default_tip(tmp_path: Path) -> None:
    fixture = build_runtime_fixture(tmp_path)
    side_sha = commit_file(fixture.merged, "side.txt", "side\n", "side commit")

    proc, receipt = fixture.run(side_sha)

    assert proc.returncode == 1
    assert receipt["status"] == "failed"
    assert receipt["reason_code"] == "landing_not_on_default_tip"
    assert git(fixture.runtime, "rev-parse", "HEAD") == fixture.initial_sha


def test_reconcile_rejects_pr_head_as_landing_sha(tmp_path: Path) -> None:
    fixture = build_runtime_fixture(tmp_path)

    proc, receipt = fixture.run(fixture.head_sha)

    assert proc.returncode == 1
    assert receipt["status"] == "failed"
    assert receipt["reason_code"] == "landing_not_on_first_parent"
    assert git(fixture.runtime, "rev-parse", "HEAD") == fixture.initial_sha


def test_reconcile_rejects_helper_not_matching_landing(tmp_path: Path) -> None:
    fixture = build_runtime_fixture(tmp_path)
    fixture.script.write_text(fixture.script.read_text() + "\n")

    proc, receipt = fixture.run()

    assert proc.returncode == 1
    assert receipt["status"] == "failed"
    assert receipt["reason_code"] == "helper_landing_source_mismatch"
    assert receipt["helper_source_verified"] is False
    assert git(fixture.runtime, "rev-parse", "HEAD") == fixture.initial_sha


def test_reconcile_provenance_ignores_clean_filter_masking(tmp_path: Path) -> None:
    fixture = build_runtime_fixture(tmp_path)
    canonical_helper = tmp_path / "canonical-helper.py"
    canonical_helper.write_bytes(fixture.script.read_bytes())
    filter_script = tmp_path / "mask-helper.sh"
    filter_script.write_text(
        "#!/bin/sh\n"
        f"cat {shlex.quote(str(canonical_helper))}\n"
    )
    filter_script.chmod(0o755)
    git(fixture.merged, "config", "filter.mask.clean", str(filter_script))
    attributes_path = Path(
        git(
            fixture.merged,
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "info/attributes",
        )
    )
    attributes_path.parent.mkdir(parents=True, exist_ok=True)
    attributes_path.write_text(f"github/scripts/{SCRIPT.name} filter=mask\n")
    fixture.script.write_text(fixture.script.read_text() + "\n# altered execution bytes\n")

    proc, receipt = fixture.run()

    assert proc.returncode == 1
    assert receipt["status"] == "failed"
    assert receipt["reason_code"] == "helper_landing_source_mismatch"
    assert receipt["helper_source_verified"] is False
    assert git(fixture.runtime, "rev-parse", "HEAD") == fixture.initial_sha


def test_reconcile_rejects_helper_changed_after_landing(tmp_path: Path) -> None:
    fixture = build_runtime_fixture(tmp_path)
    landing_helper = fixture.landing / "github" / "scripts" / SCRIPT.name
    landing_helper.write_text(landing_helper.read_text() + "\n")
    git(fixture.landing, "add", f"github/scripts/{SCRIPT.name}")
    git(fixture.landing, "commit", "-m", "change runtime helper")
    latest_sha = git(fixture.landing, "rev-parse", "HEAD")
    git(fixture.landing, "push", "origin", "main")

    proc, receipt = fixture.run()

    assert proc.returncode == 1
    assert receipt["status"] == "failed"
    assert receipt["reason_code"] == "helper_tip_source_mismatch"
    assert receipt["fetched_sha"] == latest_sha
    assert git(fixture.runtime, "rev-parse", "HEAD") == fixture.initial_sha


def test_reconcile_preserves_ignored_runtime_file_collision(tmp_path: Path) -> None:
    fixture = build_runtime_fixture(tmp_path)
    runtime_secret = fixture.runtime / "runtime-secret.txt"
    runtime_secret.write_text("local secret\n")
    tracked_secret = fixture.landing / "runtime-secret.txt"
    tracked_secret.write_text("tracked replacement\n")
    git(fixture.landing, "add", "-f", "runtime-secret.txt")
    git(fixture.landing, "commit", "-m", "track formerly ignored runtime file")
    collision_sha = git(fixture.landing, "rev-parse", "HEAD")
    git(fixture.landing, "push", "origin", "main")

    proc, receipt = fixture.run(collision_sha)

    assert proc.returncode == 1
    assert receipt["status"] == "failed"
    assert receipt["reason_code"] == "fast_forward_failed"
    assert receipt["runtime_mutated"] is False
    assert runtime_secret.read_text() == "local secret\n"
    assert git(fixture.runtime, "rev-parse", "HEAD") == fixture.initial_sha


def test_reconcile_skips_unrelated_runtime_binding(tmp_path: Path) -> None:
    fixture = build_runtime_fixture(tmp_path / "fixture")
    unrelated = tmp_path / "unrelated"
    unrelated_home = tmp_path / "unrelated-home"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(unrelated)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    configure_git(unrelated)
    commit_file(unrelated, "unrelated.txt", "unrelated\n", "unrelated")
    unrelated_home.mkdir()
    (unrelated_home / "skills").symlink_to(unrelated, target_is_directory=True)

    proc, receipt = fixture.run(code_home=unrelated_home)

    assert proc.returncode == 0
    assert receipt["status"] == "not_applicable"
    assert receipt["reason_code"] == "runtime_repo_mismatch"
    assert receipt["applicable"] is False


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__]))
