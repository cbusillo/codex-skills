#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import catalog_landing as landing
from check_operation import GateFailed, Operation
from skills.github.scripts import github_api


class CatalogLandingTests(unittest.TestCase):
    def setUp(self) -> None:
        metadata = patch.dict(vars(github_api.github_catalog_health), {"marker_fields": lambda: "catalog=unknown"})
        metadata.start()
        self.addCleanup(metadata.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config: dict[str, Any] = {"repo": "owner/catalog", "issue": 1, "source_worktree": str(self.root),
                                       "runtime_worktree": str(self.root), "ledger": str(self.root / "ledger.json"),
                                       "supervisor_session": "controller"}
        self.original_run = Operation.run
        runtime = patch.object(Operation, "run", side_effect=lambda gate, *args, **kwargs:
                               {"ok": True} if gate.startswith("supersede_") else {"status": "already_current", "after_sha": "tip"})
        self.runner = runtime.start()
        self.addCleanup(runtime.stop)
        reconcile = patch.object(landing, "reconcile_runtime", return_value=(self.root, "tip"))
        reconcile.start()
        self.addCleanup(reconcile.stop)

    def test_discovery_preserves_first_parent_order_and_refuses_missing_cursor(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        commits = []
        for number in range(3):
            subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                            "commit", "-q", "--allow-empty", "-m", str(number)], check=True)
            commits.append(landing.git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(landing.pending_landings(self.root, commits[0], commits[-1]), commits[1:])
        with self.assertRaises(GateFailed):
            landing.pending_landings(self.root, "absent", commits[-1])

    def test_interrupted_bootstrap_retains_its_landing_and_posts_before_advancing(self) -> None:
        with (patch.object(landing, "pending_landings", return_value=["first"]) as discover,
              patch.object(landing, "landing_pull", return_value=2),
              patch.object(landing, "git", return_value="script.py"),
              patch.object(landing, "verify", return_value={"ok": True, "runtime_sha": "tip"}) as verify,
              patch.object(landing, "post_result", side_effect=[GateFailed("report", "interrupted"), "comment"]) as post):
            with self.assertRaises(GateFailed):
                landing.poll(self.config, self.root)
            self.assertIsNone(json.loads((self.root / "cursor.json").read_text())["last_sha"])
            self.assertTrue(landing.poll(self.config, self.root)["ok"])
            self.assertEqual(discover.call_count, 1)
            self.assertEqual(verify.call_count, 1)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(json.loads((self.root / "cursor.json").read_text())["last_sha"], "first")

    def test_failed_harness_resumes_same_batch_only_on_explicit_recovery(self) -> None:
        with (patch.object(landing, "pending_landings", return_value=["first", "second"]),
              patch.object(landing, "landing_pull", return_value=2),
              patch.object(landing, "git", return_value="script.py"),
              patch.object(landing, "verify", side_effect=[{"ok": False, "gate": "smoke"}, {"ok": True}]) as verify,
              patch.object(landing, "post_result", return_value="comment")):
            self.assertFalse(landing.poll(self.config, self.root)["ok"])
            self.assertFalse(landing.poll(self.config, self.root)["ok"])
            self.assertEqual(verify.call_count, 1)
            result = landing.poll(self.config, self.root, resume_failed=True)
            self.assertTrue(result["ok"])
            self.assertEqual(result["landings"], ["first", "second"])
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(json.loads((self.root / "cursor.json").read_text())["last_sha"], "second")

    def test_only_human_documentation_skips_smoke_after_runtime_reconciliation(self) -> None:
        for index, files in enumerate(("README.md\ndocs/guide.md", "skills/thing/SKILL.md", "instructions/global.md", "README.md\nscript.py")):
            with (self.subTest(files=files), patch.object(landing, "pending_landings", return_value=["first", "second"]),
                  patch.object(landing, "landing_pull", return_value=2), patch.object(landing, "git", return_value=files),
                  patch.object(landing, "reconcile_runtime", return_value=(self.root, "tip")) as reconcile,
                  patch.object(landing, "verify_smoke", return_value="handoff") as smoke,
                  patch.object(landing, "post_result", return_value="comment")):
                evidence = self.root / str(index)
                result = landing.poll(self.config, evidence)
                self.assertTrue(result["ok"])
                self.assertEqual(result["landings"], ["first", "second"])
                reconcile.assert_called_once()
                self.assertEqual(smoke.call_count, 0 if files.startswith("README.md\ndocs/") else 2)

    def test_completed_batch_checks_again_without_repeating_smoke_or_posting(self) -> None:
        with (patch.object(landing, "pending_landings", side_effect=[["first", "second"], []]),
              patch.object(landing, "landing_pull", return_value=2), patch.object(landing, "git", return_value="script.py"),
              patch.object(landing, "verify", return_value={"ok": True}) as verify,
              patch.object(landing, "post_result", return_value="comment") as post):
            self.assertEqual(landing.poll(self.config, self.root)["landings"], ["first", "second"])
            self.assertEqual(landing.poll(self.config, self.root)["landings"], [])
            self.assertEqual(verify.call_count, 1)
            self.assertEqual(post.call_count, 1)

    def test_discovery_failures_remain_failed_without_saving_a_sticky_verification(self) -> None:
        with patch.object(landing, "pending_landings", side_effect=[GateFailed("discovery", "unavailable"), []]):
            with self.assertRaises(GateFailed):
                landing.poll(self.config, self.root)
            self.assertFalse((self.root / "cursor.json").exists())
            self.assertTrue(landing.poll(self.config, self.root)["ok"])


    def test_supersede_preserves_failed_attempt_and_includes_new_fix_only_after_closeout(self) -> None:
        with (patch.object(landing, "pending_landings", side_effect=[["B"], ["C"]]),
              patch.object(landing, "landing_pull", return_value=2), patch.object(landing, "git", return_value="script.py"),
              patch.object(landing, "verify", side_effect=[{"ok": False, "gate": "smoke_acknowledgement"}, {"ok": True}]) as verify,
              patch.object(landing, "post_result", return_value="comment"),
              patch.object(landing, "require_closed_smokes", side_effect=[GateFailed("closeout", "unfinished"), None]) as closeout):
            self.assertFalse(landing.poll(self.config, self.root)["ok"])
            old = verify.call_args.args[1]
            retained = {path: path.read_bytes() for path in old.evidence.glob("*.json")}
            with self.assertRaises(GateFailed):
                landing.poll(self.config, self.root, supersede_failed=True)
            self.assertEqual(verify.call_count, 1)
            result = landing.poll(self.config, self.root, supersede_failed=True)
            self.assertTrue(result["ok"])
            self.assertEqual(result["landings"], ["B", "C"])
            self.assertEqual(json.loads((self.root / "cursor.json").read_text())["last_sha"], "C")
            self.assertEqual({path: path.read_bytes() for path in old.evidence.glob("*.json")}, retained)
            self.assertNotEqual(verify.call_args.args[1].evidence, old.evidence)
            self.assertEqual(closeout.call_count, 2)

    def test_unknown_report_is_settled_by_exact_read_without_another_write(self) -> None:
        operation = Operation(self.root, ["report"])
        result = {"ok": True, "landings": ["landing"], "runtime_sha": "runtime"}
        with patch.object(Operation, "run", self.original_run), patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("comment", 360)):
            with self.assertRaises(GateFailed):
                landing.post_result(self.config, operation, result)
        report = (self.root / "result.md").read_text()
        posted = {"user": {"login": "agent[bot]"}, "body": report, "html_url": "original-comment"}
        with (patch.object(Operation, "run", self.original_run), patch.object(subprocess, "run") as write,
              patch.object(landing.github_identity, "automation_login", return_value="agent[bot]"),
              patch.object(landing.GitHubReader, "paged_json", side_effect=[[], [posted, posted], [posted]])):
            for _ in range(2):
                with self.assertRaises(GateFailed):
                    landing.post_result(self.config, operation, result)
            self.assertEqual(landing.post_result(self.config, operation, result), posted["html_url"])
            write.assert_not_called()

    def test_prelaunch_read_failure_retries_without_explicit_resume(self) -> None:
        with (patch.object(landing, "pending_landings", return_value=["first"]),
              patch.object(landing, "landing_pull", side_effect=[GateFailed("read", "unavailable"), 2]),
              patch.object(landing, "git", return_value="script.py"),
              patch.object(landing, "verify", return_value={"ok": True}) as verify,
              patch.object(landing, "post_result", return_value="comment")):
            with self.assertRaises(GateFailed):
                landing.poll(self.config, self.root)
            self.assertFalse((self.root / "first/result.json").exists())
            self.assertTrue(landing.poll(self.config, self.root)["ok"])
            verify.assert_called_once()

    def test_smoke_records_both_runtime_observations_without_claiming_loaded_version(self) -> None:
        with patch.object(landing, "verify_smoke", return_value="handoff"), patch.object(landing, "git", return_value="after"):
            result = landing.verify({**self.config, "landing_sha": "target"}, Operation(self.root, ["runtime"]), self.root, "before")
            self.assertEqual((result["runtime_sha"], result["runtime_after_sha"]), ("before", "after"))
            self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
