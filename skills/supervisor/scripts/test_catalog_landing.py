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


class CatalogLandingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config: dict[str, Any] = {
            "repo": "owner/catalog",
            "issue": 1,
            "source_worktree": str(self.root),
            "failure_argv": ["existing-sender"],
        }

    def test_discovery_preserves_first_parent_order_and_refuses_missing_cursor(self) -> None:
        with patch.object(landing, "git", side_effect=["", "tip", "tip\nbatch\nprevious\nolder"]):
            self.assertEqual(landing.pending_landings(self.root, "previous"), ["batch", "tip"])
        with patch.object(landing, "git", side_effect=["", "tip", "tip\nother"]):
            with self.assertRaises(GateFailed):
                landing.pending_landings(self.root, "previous")

    def test_interrupted_bootstrap_retains_its_landing_and_posts_before_advancing(self) -> None:
        with (
            patch.object(landing, "pending_landings", return_value=["first"]) as discover,
            patch.object(landing, "landing_pull", return_value=2),
            patch.object(landing, "git", return_value="script.py"),
            patch.object(landing, "verify", return_value={"ok": True, "runtime_sha": "tip"}) as verify,
            patch.object(landing, "post_result", side_effect=[GateFailed("report", "interrupted"), "comment"]) as post,
            patch.object(landing, "alert_failure"),
        ):
            with self.assertRaises(GateFailed):
                landing.poll(self.config, self.root)
            self.assertIsNone(json.loads((self.root / "cursor.json").read_text())["last_sha"])
            self.assertTrue(landing.poll(self.config, self.root)["ok"])
            self.assertEqual(discover.call_count, 1)
            self.assertEqual(verify.call_count, 1)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(json.loads((self.root / "cursor.json").read_text())["last_sha"], "first")

    def test_failed_harness_alerts_and_resumes_same_journal_only_on_explicit_recovery(self) -> None:
        with (
            patch.object(landing, "pending_landings", return_value=["first", "second"]),
            patch.object(landing, "landing_pull", return_value=2),
            patch.object(landing, "git", return_value="script.py"),
            patch.object(landing, "verify", side_effect=[{"ok": False, "gate": "smoke"}, {"ok": True}, {"ok": True}]) as verify,
            patch.object(landing, "post_result", return_value="comment"),
            patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "sent", "")) as sender,
        ):
            self.assertFalse(landing.poll(self.config, self.root)["ok"])
            self.assertFalse(landing.poll(self.config, self.root)["ok"])
            self.assertEqual(verify.call_count, 1)
            self.assertEqual(sender.call_count, 1)
            self.assertTrue(landing.poll(self.config, self.root, resume_failed=True)["ok"])
            self.assertEqual(verify.call_count, 3)
            self.assertEqual(json.loads((self.root / "cursor.json").read_text())["last_sha"], "second")
            self.assertEqual(sender.call_count, 1)

    def test_documentation_skip_requires_runtime_reconciliation(self) -> None:
        with (
            patch.object(landing, "reconcile_runtime", return_value=(self.root, "runtime")) as reconcile,
            patch.object(landing, "verify_smoke") as smoke,
        ):
            result = landing.verify(
                {**self.config, "landing_sha": "target", "documentation_only": True}, Operation(self.root, ["docs"])
            )
            self.assertTrue(result["ok"])
            reconcile.assert_called_once()
            smoke.assert_not_called()

    def test_mixed_change_runs_harnesses_and_completed_poll_does_no_more_work(self) -> None:
        with (
            patch.object(landing, "pending_landings", side_effect=[["first", "second"], []]),
            patch.object(landing, "landing_pull", return_value=2),
            patch.object(landing, "git", side_effect=["guide.md", "guide.md\nscript.py"]),
            patch.object(landing, "reconcile_runtime", return_value=(self.root, "tip")),
            patch.object(landing, "verify_smoke", return_value="handoff") as smoke,
            patch.object(landing, "post_result", return_value="comment") as post,
            patch.object(landing, "alert_failure") as alert,
        ):
            result = landing.poll(self.config, self.root)
            self.assertEqual([row["landing_sha"] for row in result["landings"]], ["first", "second"])
            self.assertEqual(smoke.call_count, 2)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(landing.poll(self.config, self.root)["landings"], [])
            self.assertEqual(post.call_count, 2)
            alert.assert_not_called()

    def test_discovery_failure_is_reported_without_claiming_coverage(self) -> None:
        with (
            patch.object(landing, "pending_landings", side_effect=GateFailed("discovery", "unavailable")),
            patch.object(landing, "post_result", return_value="comment") as post,
            patch.object(landing, "alert_failure") as alert,
        ):
            result = landing.poll(self.config, self.root)
            self.assertFalse(result["ok"])
            self.assertEqual(post.call_args.args[2]["landings"], [])
            alert.assert_called_once()
            self.assertFalse((self.root / "cursor.json").exists())

    def test_uncertain_failure_notification_never_replays_after_restart(self) -> None:
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("sender", 120)) as sender:
            with self.assertRaises(subprocess.TimeoutExpired):
                landing.alert_failure(self.config, Operation(self.root, ["failure"]), "comment")
            with self.assertRaises(GateFailed):
                landing.alert_failure(self.config, Operation(self.root, ["failure"]), "comment")
            self.assertEqual(sender.call_count, 1)


if __name__ == "__main__":
    unittest.main()
