#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise creation and host routing using only temporary repositories and config."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from typing import Any

import dev_worktree
from dev_worktree import DEFAULT_CONFIG_RELATIVE


SCRIPT = Path(__file__).with_name("dev_worktree.py")


def patch_attr(target: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    return mock.patch.object(target, name, *args, **kwargs)


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.repo = self.base / "source repo"
        self.repo.mkdir()
        # The main directory name is also the repo slug; spaces are supported in ancestors.
        self.repo = self.repo / "sample"
        self.repo.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "DEV_WORKTREE_")) and k not in {"CODE_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR"}}
        self.env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / ".config"),
                        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        # The product clears caller GIT_* overrides. Isolate the real Git binary
        # at the fixture boundary too, without masking injected-config regressions.
        real_git = shutil.which("git", path=self.env["PATH"])
        self.assertIsNotNone(real_git)
        fixture_bin = self.base / "fixture-bin"
        fixture_bin.mkdir()
        fixture_git = fixture_bin / "git"
        fixture_git.write_text(
            f"#!{sys.executable}\nimport os, sys\n"
            "environment = os.environ.copy()\n"
            "environment.setdefault('GIT_CONFIG_NOSYSTEM', '1')\n"
            f"environment.setdefault('GIT_CONFIG_GLOBAL', {os.devnull!r})\n"
            f"os.execve({real_git!r}, [{real_git!r}, *sys.argv[1:]], environment)\n"
        )
        fixture_git.chmod(0o700)
        self.env["PATH"] = str(fixture_bin) + os.pathsep + self.env["PATH"]
        self.git("init", "-b", "main")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "Initial")
        self.initial = self.git("rev-parse", "HEAD")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "Later")
        self.source_head = self.git("rev-parse", "HEAD")

    def git(self, *args, repo=None):
        return subprocess.run(["git", "-C", str(repo or self.repo), *args], env=self.env, capture_output=True, text=True, check=True).stdout.strip()

    def run_helper(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], env=self.env, capture_output=True, text=True)

    def settings(self, value):
        path = self.base / "settings.json"
        path.write_text(json.dumps(value))
        self.env["DEV_WORKTREE_CONFIG"] = str(path)
        return path

    def assert_refused(self, result, task="task"):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn(f"refs/heads/work/{task}", self.git("for-each-ref", "--format=%(refname)").splitlines())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.source_head)

    def test_creation_retires_finished_work_before_portable_or_host_creation(self) -> None:
        for helper in (None, self.base / "host"):
            events = []
            with mock.patch.dict(dev_worktree.os.environ, self.env, clear=True), \
                    mock.patch.object(dev_worktree, "configuration", return_value=(self.base, helper)), \
                    mock.patch.object(dev_worktree, "retire_finished", side_effect=lambda *_: events.append("retire")), \
                    mock.patch.object(dev_worktree, "refresh_primary", side_effect=lambda *_: events.append("refresh")), \
                    mock.patch.object(dev_worktree, "create", side_effect=lambda *_: events.append("create")), \
                    mock.patch.object(dev_worktree.os, "execv", side_effect=lambda *_: events.append("create")):
                dev_worktree.main([str(self.repo), "new-task", self.initial])
            self.assertEqual(events[:3], ["refresh", "retire", "create"])

    def test_retirement_delegation_does_not_start_another_sweep(self) -> None:
        with mock.patch.dict(dev_worktree.os.environ, self.env, clear=True), \
                mock.patch.object(dev_worktree, "configuration", return_value=(self.base, self.base / "host")), \
                mock.patch.object(dev_worktree, "retire_finished") as sweep, \
                mock.patch.object(dev_worktree.os, "execv", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                dev_worktree.main(["retire", "--dry-run", str(self.base / "old")])
        sweep.assert_not_called()

    def test_unavailable_sweep_does_not_prevent_creation(self) -> None:
        for outcome in (subprocess.CompletedProcess([], 2, "", "unavailable"), FileNotFoundError("missing helper")):
            with mock.patch.object(dev_worktree, "configuration", return_value=(self.base, None)), \
                    mock.patch.object(dev_worktree, "refresh_primary"), \
                    mock.patch.object(dev_worktree.subprocess, "run", side_effect=[outcome]), \
                    mock.patch.object(dev_worktree, "create", return_value=self.base / "created") as create:
                self.assertEqual(dev_worktree.main([str(self.repo), "new-task", self.initial]), 0)
            create.assert_called_once()

    def prepare_upstream(self) -> None:
        remote = self.base / "remote.git"
        self.git("clone", "--bare", str(self.repo), str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.git("fetch", "origin")
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.git("branch", "--set-upstream-to=origin/main", "main")
        self.git("reset", "--hard", self.initial)

    def test_creation_refreshes_primary_without_changing_explicit_task_start(self) -> None:
        self.prepare_upstream()
        result = self.run_helper(self.repo, "task", self.initial)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.source_head)
        self.assertEqual(self.git("rev-parse", "HEAD", repo=Path(result.stdout.strip())), self.initial)

    def test_primary_runtime_bindings_use_the_reconciler_on_both_harnesses(self) -> None:
        self.prepare_upstream()
        helper = self.repo / "skills/github/scripts/reconcile-runtime-checkout.py"
        helper.parent.mkdir(parents=True)
        marker = self.base / "runtime-call.json"
        helper.write_text("import json\nfrom pathlib import Path\n"
                          "def reconcile_runtime_checkout(source, expected_repo, landing, *, timeout_seconds):\n"
                          f"    Path({str(marker)!r}).write_text(json.dumps([str(source), landing, timeout_seconds]))\n"
                          "    return {'status': 'already_current'}\n")
        broken = self.home / "broken-codex/skills"
        broken.mkdir(parents=True)
        (broken / ".git").write_text("invalid metadata")
        self.env["CODEX_HOME"] = str(broken.parent)
        bindings = (self.home / ".agents/skills/shared", self.home / ".claude/skills/shared")
        for binding in bindings:
            with self.subTest(binding=binding):
                binding.parent.mkdir(parents=True, exist_ok=True)
                binding.symlink_to(self.repo / "skills")
                with mock.patch.dict(os.environ, self.env, clear=True):
                    dev_worktree.refresh_primary(self.repo)
                args = json.loads(marker.read_text())
                self.assertEqual(Path(args[0]), self.repo)
                self.assertIsNone(args[1])
                self.assertGreater(args[2], 0)
                self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
                binding.unlink()
                marker.unlink()
        with mock.patch.dict(os.environ, self.env, clear=True):
            receipt = dev_worktree.checkout_rules().reconcile_primary_checkout(self.repo)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["failed_operation"], ValueError.__name__)
        self.assertFalse(receipt["runtime_mutated"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_runtime_mutation_receipt_survives_the_outer_read_deadline(self) -> None:
        self.prepare_upstream()
        binding = self.home / ".agents/skills/shared"
        binding.parent.mkdir(parents=True)
        binding.symlink_to(self.repo)
        rules = dev_worktree.checkout_rules()
        now = time.monotonic()
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(time, "monotonic", return_value=now) as clock:
            def completed_runtime(*_args: object, **_kwargs: object) -> dict[str, object]:
                clock.return_value = now + 100
                return {"ok": True, "runtime_mutated": True}

            with patch_attr(rules.runpy, "run_path", return_value={"reconcile_runtime_checkout": completed_runtime}):
                receipt = rules.reconcile_primary_checkout(self.repo)
        self.assertTrue(receipt["ok"])
        self.assertTrue(receipt["runtime_mutated"])

    def test_primary_change_during_fetch_is_preserved(self) -> None:
        self.prepare_upstream()
        rules = dev_worktree.checkout_rules()
        run_git = rules.run_git

        def change_branch(repo: Path, *args: str, **kwargs: object) -> object:
            result = run_git(repo, *args, **kwargs)
            if args[0] == "fetch":
                self.git("checkout", "-b", "concurrent-work")
            return result

        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(dev_worktree, "checkout_rules", return_value=rules), \
                patch_attr(rules, "run_git", side_effect=change_branch):
            dev_worktree.refresh_primary(self.repo)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertEqual(self.git("branch", "--show-current"), "concurrent-work")

    @unittest.skipUnless(os.name == "posix", "POSIX fixture lock")
    def test_busy_primary_lock_keeps_primary_and_allows_creation(self) -> None:
        import fcntl

        self.prepare_upstream()
        lock_path = self.repo / ".git/every-code-runtime-reconciliation.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with mock.patch.dict(os.environ, self.env, clear=True):
                receipt = dev_worktree.checkout_rules().reconcile_primary_checkout(self.repo)
            self.assertEqual(receipt["status"], "blocked")
            self.assertEqual(receipt["reason_code"], "runtime_reconciliation_busy")
            self.assertFalse(receipt["runtime_mutated"])
            result = subprocess.run([sys.executable, str(SCRIPT), str(self.repo), "busy", self.initial],
                                    env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertEqual(self.git("rev-parse", "HEAD", repo=Path(result.stdout.strip())), self.initial)

    def test_unavailable_checkout_rules_do_not_prevent_creation(self) -> None:
        for failure in (FileNotFoundError("partial sync"), SyntaxError("invalid helper"), ImportError("missing helper name"),
                        TypeError("older runtime API"), AttributeError("renamed runtime API")):
            with mock.patch.object(dev_worktree, "checkout_rules", side_effect=failure), \
                    mock.patch.object(dev_worktree, "configuration", return_value=(self.base, None)), \
                    mock.patch.object(dev_worktree, "retire_finished"), \
                    mock.patch.object(dev_worktree, "create", return_value=self.base / "created") as create:
                self.assertEqual(dev_worktree.main([str(self.repo), "task", self.initial]), 0)
            create.assert_called_once()

    def test_expired_primary_read_budget_does_not_report_a_mutation(self) -> None:
        self.prepare_upstream()
        rules = dev_worktree.checkout_rules()
        run_git = rules.run_git

        def expire_after_fetch(repo: Path, *args: str, **kwargs: object) -> object:
            result = run_git(repo, *args, **kwargs)
            if args[0] == "fetch":
                rules.RECONCILIATION_DEADLINE.set(time.monotonic() - 1)
            return result

        with mock.patch.dict(os.environ, self.env, clear=True), patch_attr(rules, "run_git", side_effect=expire_after_fetch):
            receipt = rules.reconcile_primary_checkout(self.repo)
        self.assertFalse(receipt["runtime_mutated"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_primary_preflight_errors_do_not_report_a_mutation(self) -> None:
        self.prepare_upstream()
        rules = dev_worktree.checkout_rules()
        for failure in (rules.GitCommandError("status"), OSError("unreadable index")):
            with mock.patch.dict(os.environ, self.env, clear=True), patch_attr(rules, "runtime_blockers", side_effect=failure):
                receipt = rules.reconcile_primary_checkout(self.repo)
            self.assertFalse(receipt["runtime_mutated"])
            self.assertIsNone(receipt["before_sha"])
            self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_creation_preserves_unsafe_primary_checkouts(self) -> None:
        self.prepare_upstream()
        for state in ("dirty", "other-branch", "diverged", "operation", "missing-upstream"):
            with self.subTest(state=state):
                self.git("checkout", "main")
                self.git("reset", "--hard", self.initial)
                self.git("branch", "--set-upstream-to=origin/main", "main")
                dirty = self.repo / "untracked"
                operation = self.repo / ".git" / "CHERRY_PICK_HEAD"
                if state == "dirty":
                    dirty.write_text("keep")
                elif state == "other-branch":
                    self.git("checkout", "-b", "other")
                elif state == "diverged":
                    self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                             "commit", "--allow-empty", "-m", "Local work")
                elif state == "operation":
                    operation.write_text(self.source_head)
                else:
                    self.git("branch", "--unset-upstream", "main")
                before = self.git("rev-parse", "HEAD")
                result = self.run_helper(self.repo, state, self.initial)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.git("rev-parse", "HEAD"), before)
                dirty.unlink(missing_ok=True)
                operation.unlink(missing_ok=True)

    def test_default_creates_branch_at_explicit_start_outside_source(self):
        result = self.run_helper(self.repo, "task", self.initial)
        self.assertEqual(result.returncode, 0, result.stderr)
        destination = Path(result.stdout.strip())
        self.assertTrue(destination.is_dir())
        self.assertTrue(destination.is_relative_to(self.home))
        self.assertFalse(destination.is_relative_to(self.repo))
        self.assertEqual(self.git("rev-parse", "HEAD", repo=destination), self.initial)
        self.assertEqual(self.git("branch", "--show-current", repo=destination), "work/task")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.source_head)

    def test_inherited_git_location_does_not_override_explicit_repository(self):
        other = self.base / "other"
        other.mkdir()
        self.git("init", repo=other)
        overrides = {
            "GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
            "GIT_COMMON_DIR": str(other / ".git"), "GIT_INDEX_FILE": str(other / "custom-index"),
        }
        self.env.update(overrides)
        result = self.run_helper(self.repo, "task", self.initial)
        for key in overrides:
            del self.env[key]
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD", repo=Path(result.stdout.strip())), self.initial)
        self.assertEqual(self.git("for-each-ref", repo=other), "")
        self.assertFalse((other / "custom-index").exists())

    def test_hook_object_environment_does_not_redirect_creation(self):
        objects = self.base / "hook-objects"
        objects.mkdir()
        for overrides in (
            {"GIT_OBJECT_DIRECTORY": str(objects)},
            {"GIT_OBJECT_DIRECTORY": str(objects),
             "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(self.repo / ".git/objects"),
             "GIT_QUARANTINE_PATH": str(objects)},
        ):
            with self.subTest(overrides=overrides):
                task = f"hook-{len(self.git('worktree', 'list').splitlines())}"
                self.env.update(overrides)
                try:
                    result = self.run_helper(self.repo, task, self.initial)
                finally:
                    for key in overrides:
                        del self.env[key]
                self.assertEqual(result.returncode, 0, result.stderr)
                destination = Path(result.stdout.strip())
                self.assertEqual(self.git("rev-parse", "HEAD", repo=destination), self.initial)
                self.assertEqual(self.git("rev-parse", "HEAD"), self.source_head)
        self.assertEqual(list(objects.iterdir()), [])

    def test_hook_alternates_cannot_supply_a_foreign_start(self):
        other = self.base / "other"
        other.mkdir()
        self.git("init", repo=other)
        self.git("-c", "user.name=Other", "-c", "user.email=other@example.invalid", "commit", "--allow-empty", "-m", "Foreign", repo=other)
        foreign = self.git("rev-parse", "HEAD", repo=other)
        self.env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(other / ".git/objects")
        try:
            result = self.run_helper(self.repo, "task", foreign)
        finally:
            del self.env["GIT_ALTERNATE_OBJECT_DIRECTORIES"]
        self.assert_refused(result)

    def test_injected_hook_config_does_not_override_repository_config(self):
        hooks = self.base / "injected-hooks"
        hooks.mkdir()
        receipt = self.base / "injected-hook-ran"
        hook = hooks / "post-checkout"
        hook.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(receipt)!r}).touch()\n")
        hook.chmod(0o700)
        config = self.base / "injected-config"
        config.write_text(f"[core]\n    hooksPath = {hooks}\n")
        for index, overrides in enumerate((
            {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(hooks)},
            {"GIT_CONFIG_PARAMETERS": f"'core.hooksPath={hooks}'"},
            {"GIT_CONFIG_GLOBAL": str(config)},
        )):
            with self.subTest(overrides=overrides):
                receipt.unlink(missing_ok=True)
                previous = self.env.copy()
                self.env.update(overrides)
                try:
                    result = self.run_helper(self.repo, f"config-{index}", self.initial)
                finally:
                    self.env = previous
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(receipt.exists(), "caller configuration ran an injected hook")
                self.assertEqual(self.git("rev-parse", "HEAD", repo=Path(result.stdout.strip())), self.initial)

        # Ordinary file-based repository hooks continue to run.
        self.git("config", "core.hooksPath", str(hooks))
        receipt.unlink(missing_ok=True)
        result = self.run_helper(self.repo, "local-config", self.initial)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(receipt.exists())

    def test_hook_ceiling_cannot_hide_destination_repository(self):
        other = self.base / "other"
        nested = other / "nested"
        nested.mkdir(parents=True)
        self.git("init", repo=other)
        self.settings({"root": str(nested / "tasks")})
        self.env["GIT_CEILING_DIRECTORIES"] = str(other)
        try:
            result = self.run_helper(self.repo, "task", "HEAD")
        finally:
            del self.env["GIT_CEILING_DIRECTORIES"]
        self.assert_refused(result)
        self.assertFalse((nested / "tasks").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX delegated-process lifetime probing")
    def test_detached_shim_cannot_repeat_inherited_request(self):
        self.check_post_create_child(same_request=True)

    @unittest.skipUnless(os.name == "posix", "POSIX delegated-process lifetime probing")
    def test_post_create_child_can_start_later_delegation(self):
        self.check_post_create_child(same_request=False)

    def check_post_create_child(self, same_request):
        host = self.base / "host"
        child = self.base / "post-create.py"
        ready = self.base / "child-ready"
        proceed = self.base / "proceed"
        receipt = self.base / "child-result.json"
        visits = self.base / "visits"
        host.write_text(
            f"#!{sys.executable}\nimport subprocess, sys\nfrom pathlib import Path\n"
            f"visits = Path({str(visits)!r})\n"
            "count = int(visits.read_text()) + 1 if visits.exists() else 1\n"
            "visits.write_text(str(count))\n"
            "if count >= 2 or sys.argv[1] == 'later':\n    raise SystemExit(0)\n"
            f"subprocess.Popen([{sys.executable!r}, {str(child)!r}], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)\n"
        )
        host.chmod(0o700)
        child.write_text(
            "import json, subprocess, time\nfrom pathlib import Path\n"
            f"Path({str(ready)!r}).touch()\n"
            "deadline = time.monotonic() + 10\n"
            f"while not Path({str(proceed)!r}).exists() and time.monotonic() < deadline:\n    time.sleep(0.01)\n"
            f"result = subprocess.run([{sys.executable!r}, {str(SCRIPT)!r}, {"first" if same_request else "later"!r}], capture_output=True, text=True, timeout=5)\n"
            # Publish by rename so the waiting reader never sees a partly written receipt.
            f"receipt = Path({str(receipt)!r}); pending = receipt.with_suffix('.next')\n"
            "pending.write_text(json.dumps([result.returncode, result.stderr])); pending.replace(receipt)\n"
        )
        self.settings({"root": str(self.base / "tasks"), "hostHelper": str(host)})
        result = self.run_helper("first")
        self.assertEqual(result.returncode, 0, result.stderr)
        # The original delegated host has exited before its unrelated child starts work.
        proceed.touch()
        deadline = time.monotonic() + 10
        while not receipt.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists())
        self.assertTrue(receipt.exists(), "post-create child did not finish")
        code, stderr = json.loads(receipt.read_text())
        if same_request:
            self.assertNotEqual(code, 0, stderr)
            self.assertEqual(int(visits.read_text()), 1)
        else:
            self.assertEqual(code, 0, stderr)
            self.assertEqual(int(visits.read_text()), 2)

    def test_configured_root_and_linked_source_keep_main_repo_name(self):
        root = self.base / "work tree root"
        self.settings({"root": str(root)})
        first = self.run_helper(self.repo, "one", "HEAD")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_helper(first.stdout.strip(), "two", self.initial)
        self.assertEqual(second.returncode, 0, second.stderr)
        destination = Path(second.stdout.strip())
        self.assertEqual(destination, root / self.repo.name / "two")
        self.assertEqual(self.git("rev-parse", "HEAD", repo=destination), self.initial)

    def test_explicit_config_override_and_tilde_root(self):
        config = self.base / "host.json"
        config.write_text(json.dumps({"root": "~/tasks"}))
        self.env["DEV_WORKTREE_CONFIG"] = str(config)
        result = self.run_helper(self.repo, "task", "HEAD")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), self.home / "tasks" / self.repo.name / "task")

    def test_default_host_config_is_discovered(self):
        root = self.base / "host tasks"
        config = self.home / DEFAULT_CONFIG_RELATIVE
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"root": str(root)}))
        result = self.run_helper(self.repo, "task", "HEAD")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), root / self.repo.name / "task")

    def test_existing_destination_file_directory_and_dangling_link_are_preserved(self):
        root = self.base / "tasks"
        self.settings({"root": str(root)})
        parent = root / self.repo.name
        parent.mkdir(parents=True)
        for task in ("file", "directory", "link"):
            with self.subTest(task=task):
                target = parent / task
                if task == "file":
                    target.write_text("preserve")
                elif task == "directory":
                    target.mkdir()
                else:
                    target.symlink_to(self.base / "absent")
                self.assert_refused(self.run_helper(self.repo, task, "HEAD"), task)
                self.assertTrue(os.path.lexists(target))
        self.assertEqual((parent / "file").read_text(), "preserve")

    def test_existing_branch_is_preserved(self):
        self.git("branch", "work/task", self.initial)
        result = self.run_helper(self.repo, "task", "HEAD")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("rev-parse", "work/task"), self.initial)

    def test_traversal_and_option_like_slugs_and_starts_refuse(self):
        for task, start in (("../escape", "HEAD"), ("nested/task", "HEAD"), ("-bad", "HEAD"), ("x.lock", "HEAD"), ("x.", "HEAD"), ("task", "--help"), ("task", "missing")):
            with self.subTest(task=task, start=start):
                self.assert_refused(self.run_helper(self.repo, task, start), task)

    def test_root_inside_source_or_another_repository_refuses(self):
        other = self.base / "other"
        other.mkdir()
        self.git("init", repo=other)
        for root in (self.repo / "nested", other / "nested"):
            with self.subTest(root=root):
                self.settings({"root": str(root)})
                self.assert_refused(self.run_helper(self.repo, "task", "HEAD"))
                self.assertFalse(root.exists())

    def test_root_containing_source_or_a_same_named_clone_refuses(self):
        for task, root in (("source", self.repo.parent), ("clone", self.base / "other")):
            with self.subTest(root=root):
                if root != self.repo.parent:
                    clone = root / self.repo.name
                    clone.mkdir(parents=True)
                    self.git("init", repo=clone)
                self.settings({"root": str(root)})
                self.assert_refused(self.run_helper(self.repo, task, "HEAD"), task)
                self.assertFalse((root / self.repo.name / task).exists())

    def test_root_inside_git_metadata_or_bare_repository_refuses(self):
        bare = self.base / "bare.git"
        bare.mkdir()
        self.git("init", "--bare", repo=bare)
        for root in (self.repo / ".git/tasks", bare / "tasks"):
            with self.subTest(root=root):
                self.settings({"root": str(root)})
                self.assert_refused(self.run_helper(self.repo, "task", "HEAD"))
                self.assertFalse(root.exists())

    def test_symlink_into_source_and_symlink_parent_refuse(self):
        alias = self.base / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        self.settings({"root": str(alias / "tasks")})
        self.assert_refused(self.run_helper(self.repo, "task", "HEAD"))
        root = self.base / "tasks"
        root.mkdir()
        (root / self.repo.name).symlink_to(self.home, target_is_directory=True)
        self.settings({"root": str(root)})
        self.assert_refused(self.run_helper(self.repo, "task", "HEAD"))
        self.assertFalse((self.home / "task").exists())

    def test_unknown_git_probe_failure_does_not_create_a_worktree(self):
        real_git = shutil.which("git", path=self.env["PATH"])
        self.assertIsNotNone(real_git)
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        standin = bin_dir / "git"
        standin.write_text(
            f"#!{sys.executable}\nimport os, sys\n"
            "if sys.argv[-1] == '--git-dir':\n"
            "    sys.stderr.write('fixture probe unavailable')\n"
            "    raise SystemExit(17)\n"
            f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n"
        )
        standin.chmod(0o700)
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env["PATH"]
        root = self.base / "tasks"
        self.settings({"root": str(root)})
        self.assert_refused(self.run_helper(self.repo, "task", "HEAD"))
        self.assertFalse(root.exists())

    def test_missing_explicit_and_malformed_config_do_not_fall_back(self):
        self.env["DEV_WORKTREE_CONFIG"] = str(self.base / "missing.json")
        self.assert_refused(self.run_helper(self.repo, "task", "HEAD"))
        del self.env["DEV_WORKTREE_CONFIG"]
        for value in ([], {"root": None}, {"root": "relative"}, {"rot": "typo"}, {"hostHelper": "/missing"}):
            with self.subTest(value=value):
                self.settings(value)
                self.assert_refused(self.run_helper(self.repo, "task", "HEAD"))
        config = self.settings({})
        config.write_text("invalid json")
        self.assert_refused(self.run_helper(self.repo, "task", "HEAD"))

    def test_host_helper_receives_literal_creation_and_retirement_args(self):
        host = self.base / "host helper"
        receipt = self.base / "args.json"
        host.write_text(f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\nPath({str(receipt)!r}).write_text(json.dumps(sys.argv[1:]))\n")
        host.chmod(0o700)
        root = self.base / "unmounted/tasks"
        config = self.settings({"root": str(root), "hostHelper": str(host)})
        before = config.read_bytes()
        for args in ((str(self.repo), "task", "HEAD~1"), ("retire", "--dry-run", str(root / "some task"))):
            with self.subTest(args=args):
                result = self.run_helper(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(receipt.read_text()), list(args))
                self.assertFalse(root.parent.exists())
        self.assertEqual(config.read_bytes(), before)

    def test_host_failure_or_missing_helper_never_uses_portable_fallback(self):
        host = self.base / "host"
        root = self.base / "unmounted/tasks"
        self.settings({"root": str(root), "hostHelper": str(host)})
        self.assert_refused(self.run_helper(self.repo, "task", "HEAD"))
        host.write_text("#!/bin/sh\nexit 7\n")
        host.chmod(0o700)
        result = self.run_helper(self.repo, "task", "HEAD")
        self.assertEqual(result.returncode, 7)
        self.assertFalse(root.parent.exists())

    def test_self_delegation_refuses(self):
        for helper in (SCRIPT, SCRIPT.with_name("dev-worktree")):
            with self.subTest(helper=helper):
                self.settings({"root": str(self.base / "tasks"), "hostHelper": str(helper)})
                self.assert_refused(self.run_helper(self.repo, "task", "HEAD"))

    def test_host_shim_cannot_delegate_back_to_catalog(self):
        shim = self.base / "shim"
        receipt = self.base / "visits"
        # Bound the fixture itself so broken recursion cannot hang the test or leave children.
        shim.write_text(
            f"#!{sys.executable}\nimport os, sys\nfrom pathlib import Path\n"
            f"receipt = Path({str(receipt)!r})\n"
            "visits = int(receipt.read_text()) + 1 if receipt.exists() else 1\n"
            "receipt.write_text(str(visits))\n"
            "if visits >= 3:\n    raise SystemExit(33)\n"
            f"os.execv({sys.executable!r}, [{sys.executable!r}, {str(SCRIPT)!r}, *sys.argv[1:]])\n"
        )
        shim.chmod(0o700)
        self.settings({"root": str(self.base / "tasks"), "hostHelper": str(shim)})
        self.assert_refused(self.run_helper(self.repo, "task", "HEAD"))
        self.assertEqual(int(receipt.read_text()), 1)

    @unittest.skipUnless(os.name == "posix", "POSIX process-group interrupt behavior")
    def test_interrupt_allows_host_cleanup_to_finish(self):
        self.check_interrupt([sys.executable, str(SCRIPT)])

    @unittest.skipUnless(os.name == "posix", "POSIX process-group interrupt behavior")
    def test_shipped_wrapper_allows_host_interrupt_cleanup_to_finish(self):
        # Use this test runner's interpreter and a temporary uv cache; never download Python.
        self.env.update(UV_PYTHON=sys.executable, UV_PYTHON_DOWNLOADS="never", UV_CACHE_DIR=str(self.base / "uv-cache"))
        self.check_interrupt([str(SCRIPT.with_name("dev-worktree"))])

    def check_interrupt(self, entrypoint):
        host = self.base / "interrupt host"
        ready = self.base / "ready"
        cleaned = self.base / "cleaned"
        host.write_text(
            f"#!{sys.executable}\n"
            "import signal, time\nfrom pathlib import Path\n"
            "def cleanup(_signal, _frame):\n"
            "    time.sleep(0.6)\n"
            f"    Path({str(cleaned)!r}).touch()\n"
            "    raise SystemExit(130)\n"
            "signal.signal(signal.SIGINT, cleanup)\n"
            f"Path({str(ready)!r}).touch()\n"
            "while True:\n    time.sleep(0.05)\n"
        )
        host.chmod(0o700)
        self.settings({"root": str(self.base / "tasks"), "hostHelper": str(host)})
        process = subprocess.Popen(
            [*entrypoint, "retire", "fixture"], env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "host did not reach its interrupt handler")
            os.killpg(process.pid, signal.SIGINT)
            process.communicate(timeout=5)
            self.assertTrue(cleaned.exists(), "host cleanup was killed before completion")
            self.assertEqual(process.returncode, 130)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()


if __name__ == "__main__":
    unittest.main()
