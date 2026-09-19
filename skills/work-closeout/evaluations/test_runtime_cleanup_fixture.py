#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""No-model acceptance test for the runtime reconciliation fixture."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_fixture as fixture_builder


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", "-C", str(repo), *args],
        env={
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        } | {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
        },
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        raise AssertionError(f"git {args[0]} failed: {result.stderr}")
    return result.stdout.strip()


class RuntimeCleanupFixtureTests(unittest.TestCase):
    def test_real_landed_helper_fast_forwards_preserves_canary_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-cleanup-fixture-") as temporary:
            suite = Path(temporary)
            artifacts = suite / "developer-artifacts"
            artifacts.mkdir()
            case_root = artifacts / "case"
            case_root.mkdir()
            catalog = suite / "catalog"
            catalog.mkdir()
            (catalog / "SKILL.md").write_text("synthetic selected catalog\n", encoding="utf-8")

            with mock.patch.object(fixture_builder, "DEVELOPER_ARTIFACTS", artifacts):
                manifest, expected = fixture_builder.create_case(case_root, catalog)

            facts = expected["facts"]
            workspace = Path(manifest["workspace"])
            runtime = workspace / "runtime-checkout"
            primary = workspace / "primary"
            merged = workspace / "merged-task"
            helper = Path(facts["helper_path"])
            canary = Path(facts["required_preservation"][0]["path"])
            code_home = workspace / "code-home"

            self.assertEqual(helper.read_bytes(), fixture_builder.HELPER_SOURCE.read_bytes())
            self.assertEqual(hashlib.sha256(helper.read_bytes()).hexdigest(), facts["helper_source_sha256"])
            self.assertEqual(git(runtime, "rev-parse", "HEAD"), facts["initial_runtime_head"])
            self.assertEqual(git(runtime, "branch", "--show-current"), "main")
            self.assertEqual(
                Path(git(runtime, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(),
                (runtime / ".git").resolve(),
            )
            self.assertEqual(
                Path(git(merged, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(),
                (runtime / ".git").resolve(),
            )
            self.assertNotEqual(
                Path(git(primary, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(),
                (runtime / ".git").resolve(),
            )
            self.assertEqual(len(git(primary, "rev-list", "--parents", "-n", "1", "HEAD").split()), 3)
            self.assertTrue((code_home / "skills").is_symlink())
            self.assertEqual((code_home / "skills").resolve(), runtime.resolve())
            self.assertEqual(canary.read_bytes(), fixture_builder.IGNORED_CANARY)
            self.assertEqual(json.loads((case_root / "case.json").read_text()), manifest)
            self.assertEqual(json.loads((case_root / "facts.json").read_text()), expected)
            self.assertNotIn("facts.json", json.dumps(manifest))
            self.assertNotIn("expected_runtime_head", (workspace / "AGENTS.md").read_text())
            record = json.loads((workspace / "records/runtime-closeout.json").read_text())
            self.assertEqual(record["merged_task_state"], {
                "current": True,
                "ide_lease": "released",
                "job_state": "finished",
                "owner": "Jordan Ellis",
                "runtime_consumers": [],
            })
            self.assertEqual(record["runtime_checkout_state"]["runtime_binding"], "active")
            self.assertEqual(
                Path(record["runtime_checkout_state"]["binding"]).resolve(),
                (code_home / "skills").resolve(),
            )

            command = [
                sys.executable,
                str(helper),
                "--merged-worktree",
                str(merged),
                "--repo",
                fixture_builder.REPOSITORY,
                "--landing-sha",
                facts["landing_sha"],
            ]
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_")
            }
            environment.update({
                "CODE_HOME": str(code_home),
                "CODEX_HOME": str(workspace / "unused-codex-home"),
                "HOME": str(workspace / "home"),
            })
            (workspace / "home").mkdir()

            first = subprocess.run(
                command,
                env=environment,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            receipt = json.loads(first.stdout)
            for key, value in facts["required_helper_receipt"].items():
                self.assertEqual(receipt[key], value)
            self.assertEqual(git(runtime, "rev-parse", "HEAD"), facts["landing_sha"])
            self.assertEqual(canary.read_bytes(), fixture_builder.IGNORED_CANARY)
            self.assertTrue(primary.is_dir())
            self.assertTrue(runtime.is_dir())

            second = subprocess.run(
                command,
                env=environment,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            second_receipt = json.loads(second.stdout)
            self.assertEqual(second_receipt["status"], "already_current")
            self.assertEqual(second_receipt["reason_code"], "runtime_current")
            self.assertEqual(second_receipt["after_sha"], facts["landing_sha"])
            self.assertEqual(canary.read_bytes(), fixture_builder.IGNORED_CANARY)


if __name__ == "__main__":
    unittest.main()
