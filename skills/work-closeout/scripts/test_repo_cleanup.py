#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Synthetic contract tests. No user repository, cache, or credential is a fixture."""

from __future__ import annotations

import copy
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cleanup_git
import cleanup_probe
import repo_cleanup as cleanup


# Public canary text for temporary fixtures; never real credentials.
FIXTURE_MARKER = "SYNTHETIC-PRIVATE-CONTENT-47129"


def no_use(_root):
    return {"state": "unknown", "method": "synthetic_unknown", "absence_proves_inactive": False}


def run_git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    return result.stdout.decode().strip()


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "POSIX probes; other platforms report UNKNOWN")
class CleanupContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cleanup-contract-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        run_git(self.repo, "init", "--initial-branch=main")
        run_git(self.repo, "config", "user.name", "Synthetic Fixture")
        run_git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "README.md").write_text("fixture source\n")
        (self.repo / ".env.example").write_text("TOKEN=example\n")
        (self.repo / ".gitignore").write_text("out/\n.env\n.env.*\n!.env.example\n*.db\n__pycache__/\n")
        run_git(self.repo, "add", "README.md", ".gitignore", ".env.example")
        run_git(self.repo, "commit", "-m", "Synthetic source")
        self.remote = self.base / "remote.git"
        run_git(self.base, "init", "--bare", "--initial-branch=main", str(self.remote))
        run_git(self.repo, "remote", "add", "origin", str(self.remote))
        run_git(self.repo, "push", "-u", "origin", "main")
        self.worktree = self.base / "worktree"
        run_git(self.repo, "worktree", "add", "-b", "work/fixture", str(self.worktree), "main")
        self.output = self.base / "output"
        self.output.mkdir()
        self.key = bytes(range(32))
        self.live_patch = patch.object(cleanup_probe, "use_probe", no_use)
        self.live_patch.start()
        self.addCleanup(self.live_patch.stop)

    def inventory(self, *roots, **kwargs):
        return cleanup.inventory(str(self.repo), [str(root) for root in roots or (self.output,)],
                                 key=self.key, content=True, **kwargs)

    @staticmethod
    def private_files(root):
        nested = root / "nested"
        nested.mkdir(parents=True)
        (nested / ".env.production").write_text(FIXTURE_MARKER)
        (nested / "private.db").write_bytes(FIXTURE_MARKER.encode())
        return nested

    def generated(self):
        root = self.output / "__pycache__"
        root.mkdir()
        (root / "module.cpython-312.pyc").write_bytes(b"synthetic bytecode")

    def test_clean_worktree_inventory_includes_nested_ignored_private_files(self):
        nested = self.private_files(self.worktree / "out")
        self.assertEqual(run_git(self.worktree, "status", "--porcelain"), "")
        result = self.inventory(self.worktree)
        self.assertTrue(result["complete"])
        root = result["roots"][0]
        self.assertEqual(root["entries"]["out/nested/.env.production"]["git"], "ignored")
        self.assertEqual(root["entries"]["out/nested/private.db"]["category"], "protected_private")
        self.assertEqual(root["entries"][".env.example"]["category"], "tracked_source")
        self.assertIn("protected_contents", root["holds"])
        self.assertEqual((nested / ".env.production").read_text(), FIXTURE_MARKER)
        self.assertFalse(result["policy"]["may_delete"])

    def test_public_output_never_contains_contents_or_keyed_fingerprints(self):
        self.private_files(self.output)
        result = self.inventory()
        public = json.dumps(cleanup.public(result))
        self.assertNotIn(FIXTURE_MARKER, public)
        self.assertNotIn(result["_key"], public)
        for item in result["roots"][0]["entries"].values():
            if "_content_tag" in item:
                self.assertNotIn(item["_content_tag"], public)
        self.assertNotIn("_url_tag", public)

    def test_manifest_is_private_expiring_exclusive_and_outside_scope(self):
        result = self.inventory()
        directory = self.base / "evidence"
        directory.mkdir(mode=0o700)
        manifest = directory / "before.json"
        cleanup.save_manifest(str(manifest), result, "synthetic verification until test teardown", 60)
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
        self.assertEqual(cleanup.load_manifest(str(manifest))["_key"], self.key.hex())
        with self.assertRaises(FileExistsError):
            cleanup.save_manifest(str(manifest), result, "must not overwrite", 60)
        manifest.chmod(0o644)
        with self.assertRaisesRegex(cleanup.ProbeError, "permissions"):
            cleanup.load_manifest(str(manifest))
        self.output.chmod(0o700)
        with self.assertRaisesRegex(cleanup.ProbeError, "inside_scan_scope"):
            cleanup.save_manifest(str(self.output / "before.json"), result, "invalid", 60)

    def test_expired_and_symlinked_manifests_are_rejected(self):
        directory = self.base / "evidence"
        directory.mkdir(mode=0o700)
        path = directory / "expired.json"
        report = self.inventory()
        cleanup.save_manifest(str(path), report, "expire", 60)
        report["expires_epoch"] = time.time() - 1
        path.write_text(json.dumps(report))
        with self.assertRaisesRegex(cleanup.ProbeError, "expired"):
            cleanup.load_manifest(str(path))
        alias = directory / "alias.json"
        alias.symlink_to(path)
        with self.assertRaises(OSError):
            cleanup.load_manifest(str(alias))

    def test_symlink_escape_is_not_followed(self):
        external = self.base / "external"
        external.mkdir()
        (external / "secret.key").write_text(FIXTURE_MARKER)
        (self.output / "escape").symlink_to(external, target_is_directory=True)
        result = self.inventory()
        root = result["roots"][0]
        self.assertEqual(list(root["entries"]), ["escape"])
        self.assertEqual(root["entries"]["escape"]["kind"], "symlink")
        self.assertIn("filesystem_boundary", root["holds"])
        self.assertNotIn(FIXTURE_MARKER, json.dumps(cleanup.public(result)))

    def test_nested_repositories_and_bare_repositories_are_excluded(self):
        nested = self.output / "nested"
        nested.mkdir()
        run_git(nested, "init")
        run_git(self.output, "init", "--bare", str(self.output / "bare"))
        report = self.inventory()
        self.assertFalse(report["complete"])
        self.assertEqual(report["roots"][0]["coverage"], "excluded")
        self.assertEqual({entry["reason"] for entry in report["roots"][0]["exclusions"]}, {"nested_repository"})
        self.assertNotIn("nested/.git/config", report["roots"][0]["entries"])

    def test_two_pass_scan_detects_file_change(self):
        path = self.output / "notes.txt"
        path.write_text("before")
        original = cleanup_probe._walk
        calls = 0

        def mutate(*args):
            nonlocal calls
            result = original(*args)
            calls += 1
            if calls == 1:
                path.write_text("after!")
            return result

        with patch.object(cleanup_probe, "_walk", mutate):
            report = self.inventory()
        self.assertFalse(report["complete"])
        self.assertEqual(report["roots"][0]["errors"][0]["code"], "scan_changed")

    def test_head_change_between_inventory_probes_invalidates_result(self):
        original = cleanup_git.snapshot
        marker = self.base / "mutated"

        def mutate(*args):
            result = original(*args)
            if not marker.exists():
                marker.touch()
                (self.repo / "README.md").write_text("concurrent source update\n")
                run_git(self.repo, "commit", "-am", "Concurrent synthetic update")
            return result

        with patch.object(cleanup_git, "snapshot", mutate):
            report = self.inventory()
        self.assertFalse(report["git_stable"])
        self.assertFalse(report["complete"])

    def test_stale_lock_and_runtime_binding_remain_holds(self):
        run_git(self.repo, "worktree", "lock", str(self.worktree), "--reason", "synthetic stale lock")
        report = self.inventory(self.worktree, preserved=[str(self.worktree)])
        root = report["roots"][0]
        self.assertIn("locked", root["holds"])
        self.assertIn("explicit_preserve", root["holds"])
        self.assertEqual(root["disposition"], "Keep")

    def test_active_skills_runtime_is_kept(self):
        home = self.base / "host"
        home.mkdir()
        (home / "skills").symlink_to(self.output, target_is_directory=True)
        with patch.dict(os.environ, {"CODE_HOME": str(home)}):
            report = self.inventory()
        self.assertIn("skills_runtime", report["roots"][0]["holds"])

    def test_a_runtime_bound_only_through_claude_code_is_kept(self):
        skills = self.base / "claude-config" / "skills"
        skills.mkdir(parents=True)
        (skills / "team-catalog").symlink_to(self.output, target_is_directory=True)
        environment = {"CODE_HOME": "", "CODEX_HOME": "", "CLAUDE_CONFIG_DIR": str(skills.parent)}
        with patch.dict(os.environ, environment):
            report = self.inventory()
        self.assertIn("skills_runtime", report["roots"][0]["holds"])
        self.assertEqual(report["roots"][0]["disposition"], "Keep")

    def test_absent_and_unreadable_roots_have_explicit_coverage(self):
        absent = self.base / "absent-volume"
        result = self.inventory(absent)
        self.assertEqual(result["roots"][0]["coverage"], "unavailable")
        with patch.object(cleanup_probe.os, "scandir", side_effect=PermissionError(13, "synthetic")):
            result = self.inventory()
        self.assertEqual(result["roots"][0]["coverage"], "permission-denied")
        self.assertIn("started_at", result["roots"][0])
        self.assertIn("finished_at", result["roots"][0])

    def test_interruption_reports_every_requested_root(self):
        original = cleanup_probe.bounded

        def interrupt(operation, arguments, timeout):
            if operation is cleanup.scan_root:
                raise KeyboardInterrupt
            return original(operation, arguments, timeout)

        with patch.object(cleanup_probe, "bounded", interrupt):
            result = self.inventory(self.output, self.base / "second", self.base / "third")
        self.assertEqual(len(result["roots"]), 3)
        self.assertEqual({root["coverage"] for root in result["roots"]}, {"timed-out"})
        self.assertFalse(result["complete"])

    def test_entry_byte_and_explicit_pruning_limits_never_report_complete(self):
        self.private_files(self.output)
        for options in ({"max_entries": 1}, {"max_bytes": 1}, {"exclude": ["nested"]}):
            with self.subTest(options=options):
                result = self.inventory(options=options)
                self.assertFalse(result["complete"])
                with self.assertRaisesRegex(cleanup.ProbeError, "coverage_incomplete"):
                    cleanup.check_manifest(result)

    def test_aliases_are_recorded_and_size_is_not_double_counted(self):
        self.generated()
        alias = self.base / "alias"
        alias.symlink_to(self.output, target_is_directory=True)
        result = self.inventory(self.output, alias)
        self.assertTrue(result["complete"])
        self.assertEqual(result["roots"][1]["alias_of"], str(self.output))
        self.assertGreater(result["roots"][0]["unique_file_bytes"], 0)
        self.assertEqual(result["roots"][1]["unique_file_bytes"], 0)

    def test_generated_output_is_a_hint_not_deletion_authority(self):
        self.generated()
        result = self.inventory()
        root = result["roots"][0]
        self.assertEqual(root["disposition"], "Possible cleanup")
        self.assertFalse(result["policy"]["may_delete"])
        self.assertIn("unobserved_app_runtime_or_ide_use", root["unresolved"])
        self.assertTrue(cleanup.check_manifest(result)["ok"])
        self.assertTrue((self.output / "__pycache__" / "module.cpython-312.pyc").exists())

    def test_unknown_local_file_blocks_whole_directory_disposal(self):
        (self.output / "valuable.data").write_text("unspecified local work")
        before = self.inventory()
        self.assertEqual(before["roots"][0]["disposition"], "Needs review")
        shutil.rmtree(self.output)  # Simulate a prior external mistake, only in this fixture.
        result = cleanup.check_manifest(before, removed=[str(self.output)])
        self.assertFalse(result["ok"])
        self.assertIn("protected_or_ambiguous_root_removed", result["errors"])

    def test_removal_of_protected_private_content_fails_postcondition(self):
        self.private_files(self.output)
        before = self.inventory()
        shutil.rmtree(self.output)
        result = cleanup.check_manifest(before, removed=[str(self.output)])
        self.assertIn("protected_or_ambiguous_root_removed", result["errors"])

    def test_revalidate_detects_content_change_even_when_size_and_mtime_match(self):
        nested = self.private_files(self.output)
        before = self.inventory()
        path = nested / "private.db"
        original = path.stat()
        path.write_bytes(b"x" * original.st_size)
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        result = cleanup.check_manifest(before)
        self.assertFalse(result["ok"])
        self.assertIn("retained_content_or_identity_changed", result["errors"])

    def test_failed_move_and_copy_without_source_removal_fail(self):
        self.private_files(self.output)
        before = self.inventory()
        destination = self.base / "destination"
        failed = cleanup.check_manifest(before, moved=[[str(self.output), str(destination)]])
        self.assertFalse(failed["ok"])
        shutil.copytree(self.output, destination)
        copied = cleanup.check_manifest(before, moved=[[str(self.output), str(destination)]])
        self.assertFalse(copied["ok"])
        self.assertIn("source_removal_unverified", copied["errors"])

    def test_recovery_copy_with_new_identity_preserves_bytes_and_permissions(self):
        self.private_files(self.output)
        before = self.inventory()
        destination = self.base / "destination"
        shutil.copytree(self.output, destination)
        self.assertNotEqual(self.output.stat().st_ino, destination.stat().st_ino)
        shutil.rmtree(self.output)
        # Model a source on another device without using real second-volume data.
        before["roots"][0]["identity"]["device"] = destination.stat().st_dev + 1
        for entry in before["roots"][0]["entries"].values():
            entry["stat"]["device"] = destination.stat().st_dev + 1
        # Copy semantics intentionally accept different device/inode identities.
        result = cleanup.check_manifest(before, moved=[[str(self.output), str(destination)]])
        self.assertTrue(result["ok"], result["errors"])
        self.assertIsNone(result["policy"]["space_reclaimed_bytes"])
        self.assertEqual((destination / "nested" / "private.db").read_text(), FIXTURE_MARKER)

    def test_tampered_recovery_or_trash_is_not_durable_preservation(self):
        self.private_files(self.output)
        before = self.inventory()
        destination = self.base / "Trash" / "destination"
        destination.parent.mkdir()
        shutil.move(str(self.output), destination)
        (destination / "nested" / "private.db").write_text("changed")
        result = cleanup.check_manifest(before, moved=[[str(self.output), str(destination)]])
        self.assertIn("trash_is_not_durable_recovery", result["errors"])
        self.assertIn("recovery_content_changed", result["errors"])

    def test_removed_output_retained_private_data_and_git_remain_verified(self):
        self.generated()
        retained = self.base / "retained"
        retained.mkdir()
        self.private_files(retained)
        before = self.inventory(self.output, retained)
        shutil.rmtree(self.output)
        result = cleanup.check_manifest(before, removed=[str(self.output)])
        self.assertTrue(result["ok"], result["errors"])
        (retained / "nested" / ".env.production").write_text("lost original")
        result = cleanup.check_manifest(before, removed=[str(self.output)])
        self.assertFalse(result["ok"])

    def test_new_git_operation_and_registration_invalidate_revalidation(self):
        before = self.inventory()
        git_dir = Path(run_git(self.worktree, "rev-parse", "--absolute-git-dir"))
        (git_dir / "index.lock").touch()
        self.assertFalse(cleanup.check_manifest(before)["ok"])
        (git_dir / "index.lock").unlink()
        run_git(self.repo, "worktree", "add", "--detach", str(self.base / "new-worktree"), "HEAD")
        self.assertFalse(cleanup.check_manifest(before)["ok"])

    def test_live_remote_coverage_does_not_trust_tracking_cache(self):
        before = self.inventory()
        self.assertEqual(before["repository"]["ref_coverage"]["refs/heads/main"]["state"], "covered")
        moved_remote = self.base / "unavailable-remote"
        self.remote.rename(moved_remote)
        result = self.inventory()
        self.assertFalse(result["complete"])
        self.assertEqual(result["repository"]["remotes"][0]["coverage"], "unavailable")

    def test_remote_urls_and_errors_do_not_echo_credentials(self):
        credentialed_url = "https://" + "synthetic:" + FIXTURE_MARKER + "@example.invalid/repo"
        run_git(self.repo, "remote", "set-url", "origin", credentialed_url)
        result = self.inventory(offline=True)
        self.assertFalse(result["complete"])
        output = json.dumps(cleanup.public(result))
        self.assertNotIn(FIXTURE_MARKER, output)
        self.assertNotIn("https://", output)

    def test_old_or_unlisted_manifest_targets_are_rejected(self):
        before = self.inventory()
        stale = copy.deepcopy(before)
        stale["created_epoch"] -= 600
        with self.assertRaisesRegex(cleanup.ProbeError, "too_old"):
            cleanup.check_manifest(stale)
        with self.assertRaisesRegex(cleanup.ProbeError, "unique_manifest_roots"):
            cleanup.check_manifest(before, removed=[str(self.base / "unlisted")])

    def test_unknown_use_is_not_clearance_and_positive_use_is_a_hold(self):
        with patch.object(cleanup_probe, "use_probe", return_value={"state": "observed", "pids": [1234]}):
            report = self.inventory()
        self.assertIn("observed_live_use", report["roots"][0]["holds"])
        self.assertFalse(report["policy"]["may_delete"])

    def test_duplicate_move_sources_and_aliased_recovery_are_rejected(self):
        before = self.inventory()
        destination = self.base / "destination"
        with self.assertRaisesRegex(cleanup.ProbeError, "unique_manifest_roots"):
            cleanup.check_manifest(before, moved=[[str(self.output), str(destination)],
                                                   [str(self.output), str(self.base / "other")]])
        shutil.move(str(self.output), destination)
        alias = self.base / "destination-alias"
        alias.symlink_to(destination, target_is_directory=True)
        result = cleanup.check_manifest(before, moved=[[str(self.output), str(alias)]])
        self.assertIn("recovery_destination_alias", result["errors"])

    def test_cli_manifest_then_revalidate_emits_no_private_contents(self):
        self.private_files(self.output)
        directory = self.base / "evidence"
        directory.mkdir(mode=0o700)
        manifest = directory / "before.json"
        script = Path(cleanup.__file__).resolve()
        command = [sys.executable, str(script), "inventory", "--repo", str(self.repo),
                   "--root", str(self.output), "--manifest", str(manifest),
                   "--purpose", "synthetic CLI round trip", "--json"]
        first = subprocess.run(command, capture_output=True, cwd=self.base, timeout=30)
        self.assertEqual(first.returncode, 0, first.stdout.decode())
        self.assertNotIn(FIXTURE_MARKER.encode(), first.stdout + first.stderr)
        self.assertFalse(json.loads(first.stdout)["policy"]["may_delete"])
        second = subprocess.run([sys.executable, str(script), "revalidate", "--before", str(manifest), "--json"],
                                capture_output=True, cwd=self.base, timeout=30)
        self.assertEqual(second.returncode, 0, second.stdout.decode())
        self.assertEqual(json.loads(second.stdout)["evidence"], "snapshot_unchanged")

    def test_special_file_manifest_cannot_claim_complete_content(self):
        os.mkfifo(self.output / "pipe")
        result = self.inventory()
        self.assertIn("filesystem_boundary", result["roots"][0]["holds"])
        self.assertFalse(result["roots"][0]["content_verified"])

    def test_retained_directory_replacement_and_file_set_changes_are_detected(self):
        self.generated()
        before = self.inventory()
        replacement = self.base / "replacement"
        shutil.copytree(self.output, replacement)
        shutil.rmtree(self.output)
        replacement.rename(self.output)
        self.assertFalse(cleanup.check_manifest(before)["ok"])
        unchanged_identity = self.inventory()
        (self.output / "added.txt").write_text("new local work")
        self.assertFalse(cleanup.check_manifest(unchanged_identity)["ok"])
        (self.output / "added.txt").unlink()
        unchanged_identity = self.inventory()
        (self.output / "__pycache__" / "module.cpython-312.pyc").unlink()
        self.assertFalse(cleanup.check_manifest(unchanged_identity)["ok"])

    def test_malformed_manifest_roots_fail_without_echoing_input(self):
        path = self.base / "malformed.json"
        path.write_text(json.dumps({"schema_version": 1, "roots": [FIXTURE_MARKER]}))
        path.chmod(0o600)
        result = subprocess.run([sys.executable, str(Path(cleanup.__file__)), "revalidate", "--before", str(path), "--json"],
                                capture_output=True, cwd=self.base, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(FIXTURE_MARKER.encode(), result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["error"], "manifest_invalid")


class ProtectedRootContracts(unittest.TestCase):
    @staticmethod
    def holds(resolved, platform):
        return any(part.lower() in cleanup.PRIVATE_PARTS for part in cleanup.named_parts(resolved, platform))

    def test_the_macos_system_directory_is_not_a_private_folder(self):
        for resolved in ("/private/var/folders/x/T/run/out", "/private/tmp/run/out", "/private/etc/app"):
            with self.subTest(resolved=resolved):
                self.assertFalse(self.holds(resolved, "darwin"))

    def test_chosen_private_folders_stay_protected(self):
        for resolved, platform in (("/Users/someone/private/out", "darwin"),
                                   ("/private/var/folders/x/T/secrets/out", "darwin"),
                                   ("/private/notes", "darwin"),
                                   ("/private", "darwin"),
                                   ("/private/var/run/out", "linux")):
            with self.subTest(resolved=resolved, platform=platform):
                self.assertTrue(self.holds(resolved, platform))


class BoundedProbeContracts(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX process groups")
    def test_hung_child_times_out_without_leaving_it_running(self):
        start = time.monotonic()
        result = cleanup_probe.bounded(time.sleep, (10,), 0.05)
        self.assertEqual(result, {"error": "timed_out"})
        self.assertLess(time.monotonic() - start, 2)

    @unittest.skipUnless(os.name == "posix", "POSIX pipe selectors")
    def test_bounded_command_does_not_echo_stderr_or_keep_unbounded_output(self):
        with self.assertRaisesRegex(cleanup.ProbeError, "output_limit"):
            cleanup_probe.command(["git", "--help"], limit=1)

    def test_unsupported_platform_reports_unknown(self):
        with patch.object(cleanup_probe.os, "name", "unsupported"):
            self.assertEqual(cleanup_probe.bounded(time.sleep, (0,), 1), {"error": "unsupported_platform"})


if __name__ == "__main__":
    unittest.main()
