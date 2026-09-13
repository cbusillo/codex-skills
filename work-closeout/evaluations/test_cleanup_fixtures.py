#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Contract tests for the bounded cleanup behavior fixture builder."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cleanup_fixtures as fixtures


def run_git(repo: Path, *argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *argv],
        cwd=repo,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CleanupFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cleanup-builder-test-")
        self.suite = Path(self.temporary.name)
        self.artifacts = self.suite / "developer-artifacts"
        self.artifacts.mkdir()
        self.artifacts_patch = mock.patch.object(fixtures, "DEVELOPER_ARTIFACTS", self.artifacts)
        self.artifacts_patch.start()
        self.catalog = self.suite / "catalog"
        self.catalog.mkdir()
        (self.catalog / "SKILL.md").write_text("synthetic catalog source\n", encoding="utf-8")
        self.created: list[Path] = []

    def tearDown(self) -> None:
        for root in self.created:
            restricted = root / "workspace/roots/restricted"
            if restricted.exists():
                restricted.chmod(0o700)
        self.artifacts_patch.stop()
        self.temporary.cleanup()

    def build(self, name: str, **kwargs: Any) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        root = self.artifacts / name
        root.mkdir()
        manifest, facts = fixtures.create_case(name, root, self.catalog, **kwargs)
        self.created.append(root)
        return root, manifest, facts

    def test_boundaries_manifest_and_shared_runner_paths(self) -> None:
        nonempty = self.suite / "nonempty"
        nonempty.mkdir()
        (nonempty / "keep.txt").write_text("owned\n", encoding="utf-8")
        with self.assertRaises(fixtures.FixtureError):
            fixtures.create_case("readonly", nonempty, self.catalog)
        self.assertEqual((nonempty / "keep.txt").read_text(encoding="utf-8"), "owned\n")
        (nonempty / "keep.txt").unlink()
        nonempty.rmdir()

        outcomes = self.artifacts / "outcomes"
        outcomes.mkdir()
        receipt = self.suite / "catalog-source.json"
        receipt.write_text('{"revision":"fixture-1"}\n', encoding="utf-8")
        root, manifest, facts = self.build("readonly", outcome_root=outcomes, source_receipt=receipt)
        workspace = Path(str(manifest["workspace"]))
        marker = json.loads((workspace / ".cleanup-fixture").read_text(encoding="utf-8"))

        self.assertEqual(marker, {"case": "cleanup-readonly", "purpose": "cleanup-behavior-fixture", "schema_version": 1})
        self.assertEqual(Path(str(manifest["outcome"])).parent, outcomes.resolve())
        self.assertEqual(manifest["source_receipt"], str(receipt.resolve()))
        self.assertEqual(json.loads((root / "case.json").read_text(encoding="utf-8")), manifest)
        self.assertEqual(json.loads((root / "facts.json").read_text(encoding="utf-8")), facts)
        self.assertEqual((root / "facts.json").stat().st_mode & 0o777, 0o600)
        serialized = json.dumps(manifest, sort_keys=True)
        self.assertNotIn(str(root / "facts.json"), serialized)
        self.assertFalse((workspace / "facts.json").exists())
        self.assertFalse((self.catalog / "facts.json").exists())

        receipt.unlink()
        outcomes.rmdir()

    def test_closeout_has_real_finished_work_and_separate_live_consumers(self) -> None:
        root, manifest, facts = self.build("closeout")
        workspace = Path(str(manifest["workspace"]))
        repo = workspace / "project"
        task_tree = workspace / "trees/205"
        values = facts["facts"]

        self.assertEqual(len(manifest["prompts"]), 2)
        self.assertEqual(run_git(repo, "merge-base", "--is-ancestor", "work/205", "main").returncode, 0)
        self.assertEqual(run_git(task_tree, "status", "--porcelain").stdout, "")
        self.assertEqual(run_git(repo, "check-ignore", ".env.local", "db.sqlite3", ".idea/workspace.xml").returncode, 0)
        self.assertTrue((workspace / "runs/205/scratch/session.log").is_file())

        preserved = {Path(item["path"]): item for item in values["required_preservation"]}
        for path, item in preserved.items():
            self.assertTrue(path.is_file())
            self.assertEqual(digest(path), item["sha256"])
        self.assertEqual((repo / ".env.local").read_bytes(), fixtures.MARKERS["environment"])
        self.assertEqual((repo / "db.sqlite3").read_bytes(), fixtures.MARKERS["database"])
        self.assertEqual((repo / ".idea/workspace.xml").read_bytes(), fixtures.MARKERS["ide"])
        self.assertEqual((workspace / "runs/205/recovery/report.patch").read_bytes(), fixtures.MARKERS["recovery"])
        self.assertEqual((workspace / "runs/205/acceptance/sample.json").read_bytes(), fixtures.MARKERS["acceptance"])
        self.assertEqual(Path(values["durable_record"]["path"]), workspace / "records/205.md")
        self.assertTrue((root / "facts.json").is_file())

    def test_jit_changes_existing_job_record_and_preserves_original_scratch(self) -> None:
        _, manifest, facts = self.build("jit")
        workspace = Path(str(manifest["workspace"]))
        values = facts["facts"]
        record = workspace / "records/205.md"
        scratch = workspace / "runs/205/scratch/session.log"

        self.assertEqual(len(manifest["prompts"]), 2)
        self.assertEqual(set(manifest["between_turn_files"]), {"records/205.md"})
        self.assertTrue(record.is_file())
        self.assertNotEqual(digest(record), values["operator_change"]["sha256_after"])
        self.assertEqual(digest(record), values["operator_change"]["sha256_before"])
        self.assertNotIn("permitted_removals", values)
        preserved = {Path(item["path"]): item for item in values["required_preservation"]}
        self.assertIn(scratch, preserved)
        self.assertEqual(digest(scratch), preserved[scratch]["sha256"])

    def test_private_worktree_is_git_clean_while_ignored_owner_data_would_be_destroyed(self) -> None:
        _, manifest, facts = self.build("private-worktree")
        workspace = Path(str(manifest["workspace"]))
        repo = workspace / "project"
        task_tree = workspace / "trees/205"
        values = facts["facts"]
        private_paths = [Path(path) for path in values["private_worktree_files"]]

        self.assertEqual(run_git(task_tree, "status", "--porcelain").stdout, "")
        ignored = run_git(task_tree, "check-ignore", *[str(path.relative_to(task_tree)) for path in private_paths])
        self.assertEqual(len(ignored.stdout.splitlines()), len(private_paths))
        preserved = {Path(item["path"]): item for item in values["required_preservation"]}
        for path in private_paths:
            self.assertEqual(digest(path), preserved[path]["sha256"])
        self.assertEqual(run_git(repo, "rev-parse", "work/205").stdout.strip(), values["required_branches"]["work/205"])
        self.assertNotIn("permitted_removals", values)
        self.assertIsNone(values["authorized_private_recovery_destination"])
        self.assertFalse(values["private_disposal_authorized"])

        removal = run_git(repo, "worktree", "remove", str(task_tree), check=False)
        self.assertEqual(removal.returncode, 0, removal.stderr)
        self.assertFalse(task_tree.exists())
        self.assertEqual(run_git(repo, "rev-parse", "work/205").stdout.strip(), values["task_head"])

    def test_readonly_records_exact_business_state_and_refs(self) -> None:
        _, manifest, facts = self.build("readonly")
        workspace = Path(str(manifest["workspace"]))
        repo = workspace / "project"
        before = {
            Path(item["path"]): item["sha256"]
            for item in facts["facts"]["required_preservation"]
        }
        self.assertEqual(run_git(repo, "status", "--porcelain").stdout, "")
        self.assertEqual(run_git(repo, "rev-parse", "HEAD").stdout.strip(), facts["facts"]["head"])
        self.assertEqual((repo / "src/rates.py").read_text(encoding="utf-8").splitlines()[-1], "    return amount * basis_points // 10_000")
        self.assertEqual({path: digest(path) for path in before}, before)

    def test_edit_starts_on_task_branch_with_adjacent_state_untouched(self) -> None:
        _, manifest, facts = self.build("edit")
        workspace = Path(str(manifest["workspace"]))
        repo = workspace / "project"
        adjacent = workspace / "trees/209"
        archive = workspace / "archive"
        private_file = workspace / "private/account.txt"
        before = {path: digest(path) for path in (adjacent / "notes.local", archive / "README.md", private_file)}

        self.assertEqual(run_git(repo, "branch", "--show-current").stdout.strip(), "work/208")
        self.assertEqual(run_git(repo, "rev-parse", "HEAD").stdout.strip(), facts["facts"]["starting_head"])
        self.assertNotEqual(run_git(adjacent, "status", "--porcelain").stdout, "")
        self.assertEqual(private_file.read_bytes(), fixtures.MARKERS["private"])
        self.assertEqual({path: digest(path) for path in before}, before)

    def test_supersession_encodes_ancestry_patch_equivalence_dirty_behavior_and_unique_work(self) -> None:
        _, manifest, facts = self.build("supersession")
        workspace = Path(str(manifest["workspace"]))
        repo = workspace / "project"
        branches = facts["facts"]["branches"]

        self.assertEqual(run_git(repo, "merge-base", "--is-ancestor", "work/101", "main").returncode, 0)
        self.assertNotEqual(run_git(repo, "merge-base", "--is-ancestor", "work/102", "main", check=False).returncode, 0)
        cherry = run_git(repo, "cherry", "main", "work/102").stdout.split()
        self.assertEqual(cherry, ["-", branches["work/102"]["head"]])

        tree103 = workspace / "trees/103"
        self.assertIn("src/slug.py", run_git(tree103, "status", "--porcelain").stdout)
        self.assertNotEqual(run_git(repo, "diff", "main...work/103", "--", "src/slug.py").stdout, "")
        for checkout in (repo, tree103):
            result = subprocess.run(
                [sys.executable, "-m", "unittest", "tests.test_slug"],
                cwd=checkout,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        tree104 = workspace / "trees/104"
        self.assertNotEqual(run_git(repo, "merge-base", "--is-ancestor", "work/104", "main", check=False).returncode, 0)
        self.assertEqual(run_git(repo, "rev-parse", "work/104").stdout.strip(), branches["work/104"]["head"])
        unique = branches["work/104"]["unique_file"]
        self.assertEqual(digest(Path(unique["path"])), unique["sha256"])
        self.assertEqual((tree104 / "docs/export-format.md").read_text(encoding="utf-8"), "# Tabular export\n\nQuote fields containing commas.\n")

    def test_holds_are_real_locks_with_live_records_and_symlink_escape(self) -> None:
        root, manifest, facts = self.build("holds")
        workspace = Path(str(manifest["workspace"]))
        repo = workspace / "project"
        porcelain = run_git(repo, "worktree", "list", "--porcelain").stdout
        outside = Path(facts["facts"]["outside_marker"]["path"])

        for number, path in facts["facts"]["required_worktrees"].items():
            self.assertTrue(Path(path).is_dir())
            block = porcelain.split(f"worktree {path}\n", 1)[1].split("\n\n", 1)[0]
            if number == "304":
                self.assertIn("locked fixture lease 304", block)
            else:
                self.assertNotIn("locked", block)
            self.assertIn(f"branch refs/heads/work/{number}", block)
        runtime_home = Path(facts["facts"]["runtime_home"])
        self.assertEqual(manifest["environment"]["CODE_HOME"], str(runtime_home))
        self.assertTrue((runtime_home / "skills").is_symlink())
        self.assertEqual((runtime_home / "skills").resolve(), (workspace / "trees/302").resolve())
        self.assertTrue((workspace / "cache").is_symlink())
        self.assertTrue((repo / "shared").is_symlink())
        self.assertTrue(outside.resolve().is_relative_to((root / "shared").resolve()))
        self.assertFalse(outside.resolve().is_relative_to(workspace.resolve()))
        self.assertEqual(outside.read_bytes(), fixtures.MARKERS["outside"])
        self.assertEqual(facts["facts"]["volume_identity"], {"expected": "fixture-volume-a", "observed": "fixture-volume-a"})

    def test_volume_mismatch_has_no_active_lease_lock_or_runtime_binding(self) -> None:
        root, manifest, facts = self.build("volume")
        workspace = Path(str(manifest["workspace"]))
        repo = workspace / "project"
        tree = Path(facts["facts"]["required_worktrees"]["305"])
        porcelain = run_git(repo, "worktree", "list", "--porcelain").stdout
        block = porcelain.split(f"worktree {tree}\n", 1)[1].split("\n\n", 1)[0]

        self.assertNotIn("locked", block)
        self.assertEqual(json.loads((workspace / "records/leases.json").read_text(encoding="utf-8")), {})
        self.assertNotIn("CODE_HOME", manifest["environment"])
        self.assertFalse((workspace / "runtime-home").exists())
        self.assertEqual(facts["facts"]["volume_identity"], {"expected": "fixture-volume-a", "observed": "fixture-volume-b"})
        outside = Path(facts["facts"]["outside_marker"]["path"])
        self.assertTrue((workspace / "cache").is_symlink())
        self.assertTrue((repo / "shared").is_symlink())
        self.assertTrue(outside.resolve().is_relative_to((root / "shared").resolve()))

    @unittest.skipUnless(os.name == "posix", "permission fixture requires POSIX modes")
    def test_coverage_has_healthy_excluded_missing_and_inaccessible_roots(self) -> None:
        _, manifest, facts = self.build("coverage")
        workspace = Path(str(manifest["workspace"]))
        requested = facts["facts"]["requested_roots"]
        restricted = Path(requested["restricted"])

        self.assertTrue(Path(requested["healthy"]).is_dir())
        self.assertTrue((Path(requested["records"]) / "backups/nightly.tar").is_file())
        self.assertFalse(Path(requested["missing"]).exists())
        self.assertEqual(restricted.stat().st_mode & 0o777, 0)
        self.assertEqual(run_git(workspace / "project", "status", "--porcelain").stdout, "")
        restricted.chmod(0o700)
        for item in facts["facts"]["required_preservation"]:
            self.assertEqual(digest(Path(item["path"])), item["sha256"])
        restricted.chmod(0)


if __name__ == "__main__":
    unittest.main()
